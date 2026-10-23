"""
Парсер новых сообщений из Telegram-каналов и групп, которые лежат в папке (по умолчанию crypto).

Работает через user API (MTProto, Telethon) от имени вашего аккаунта
и пишет сообщения в локальную SQLite.

Команды:
    python -m tg_parser login      # один раз: вход по номеру и коду
    python -m tg_parser channels   # показать состав папки и что из него парсится
    python -m tg_parser            # слушать новые сообщения (по умолчанию)
"""

import argparse
import asyncio
import logging
import sys

from telethon import TelegramClient, events
from telethon.tl.types import MessageService

from . import metrics
from .config import Config, ConfigError, load_config
from .folder import PARSED_KINDS, chat_kind, load_folder_dialogs, load_folder_targets
from .history import history_loop
from .proxy import describe_proxy, parse_proxy
from .storage import Storage, message_row

log = logging.getLogger("tg_parser")


def make_client(cfg: Config) -> TelegramClient:
    cfg.session_path.parent.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        str(cfg.session_path),
        cfg.api_id,
        cfg.api_hash,
        connection_retries=None,  # переподключаться бесконечно
        retry_delay=5,
        **parse_proxy(cfg.proxy_url),
    )


async def connect_authorized(cfg: Config) -> TelegramClient:
    client = make_client(cfg)
    log.info("подключение к Telegram (%s)", describe_proxy(cfg.proxy_url))
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise SystemExit(
            "сессия не авторизована: сначала выполните вход командой `login` (см. README)"
        )
    return client


async def cmd_login(cfg: Config) -> None:
    client = make_client(cfg)
    log.info("вход в Telegram (%s)", describe_proxy(cfg.proxy_url))
    await client.start(phone=cfg.phone or (lambda: input("Номер телефона: ")))
    me = await client.get_me()
    log.info("вход выполнен: %s (id=%s), сессия: %s", me.username or me.first_name, me.id, cfg.session_path)
    await client.disconnect()


KIND_TITLES = {
    "channel": "канал",
    "supergroup": "супергруппа",
    "group": "группа",
    "bot": "бот",
    "user": "пользователь",
    "saved": "избранное",
    "other": "другое",
}


def print_dialogs(title: str, dialogs: list) -> None:
    print(f"\n{title}: {len(dialogs)}")
    for d in sorted(dialogs, key=lambda d: (chat_kind(d.entity), d.name.casefold())):
        username = getattr(d.entity, "username", None)
        username = f"@{username}" if username else "-"
        print(f"  {d.id:>16}  {KIND_TITLES[chat_kind(d.entity)]:<12}  {username:<32}  {d.name}")


async def cmd_channels(cfg: Config) -> None:
    client = await connect_authorized(cfg)
    try:
        dialogs = await load_folder_dialogs(client, cfg.folder)
    finally:
        await client.disconnect()
    targets = [d for d in dialogs if chat_kind(d.entity) in PARSED_KINDS]
    others = [d for d in dialogs if chat_kind(d.entity) not in PARSED_KINDS]
    print(f"Папка {cfg.folder!r}: {len(dialogs)} чатов")
    print_dialogs("Парсятся (каналы и группы)", targets)
    if others:
        print_dialogs("Не парсятся (личные чаты, боты, избранное)", others)


async def cmd_run(cfg: Config) -> None:
    storage = Storage(cfg.db_path)
    client = await connect_authorized(cfg)
    chats: dict[int, object] = {}
    if cfg.metrics_port:
        metrics.start(cfg.metrics_port, cfg.db_path, client.is_connected)
        log.info("метрики: http://0.0.0.0:%d/metrics", cfg.metrics_port)

    async def refresh_chats() -> None:
        new = await load_folder_targets(client, cfg.folder)
        added, removed = new.keys() - chats.keys(), chats.keys() - new.keys()
        chats.clear()
        chats.update(new)
        for kind in PARSED_KINDS:
            metrics.TRACKED_CHATS.labels(kind=kind).set(sum(chat_kind(c) == kind for c in new.values()))
        if added or removed:
            log.info("папка %r: %d чатов (+%d, -%d)", cfg.folder, len(new), len(added), len(removed))

    def save(message, source: str) -> None:
        chat = chats.get(message.chat_id)
        row = message_row(message, chat)
        if storage.save(row):
            metrics.record_saved(row, source)
            title = getattr(chat, "title", message.chat_id)
            preview = (message.message or "").replace("\n", " ")[:80]
            log.info("[%s] #%d %s", title, message.id, preview)

    @client.on(events.NewMessage(func=lambda e: e.chat_id in chats))
    async def on_new_message(event):
        save(event.message, "live")

    async def catch_up() -> None:
        # добираем то, что пришло, пока парсер был выключен
        for peer_id, entity in list(chats.items()):
            last_id = storage.last_message_id(peer_id)
            if last_id is None:
                continue  # чат новый: историю не качаем, только новые сообщения
            async for message in client.iter_messages(
                entity, min_id=last_id, reverse=True, limit=cfg.catchup_limit
            ):
                # сервисные («закрепил», «вступил») NewMessage тоже не присылает
                if not isinstance(message, MessageService):
                    save(message, "catchup")

    async def refresh_loop() -> None:
        while True:
            await asyncio.sleep(cfg.folder_refresh_seconds)
            try:
                await refresh_chats()
            except Exception:
                metrics.ERRORS.labels(stage="folder_refresh").inc()
                log.exception("не удалось обновить список чатов, оставляю прежний")

    try:
        await refresh_chats()
        if not chats:
            log.warning("в папке %r нет каналов и групп, жду, пока они появятся", cfg.folder)
        await catch_up()
        log.info("слушаю новые сообщения из %d чатов, база: %s", len(chats), cfg.db_path)
        tasks = [asyncio.create_task(refresh_loop())]
        if cfg.history_days:
            tasks.append(asyncio.create_task(
                history_loop(client, storage, chats, cfg.history_days, cfg.history_delay_seconds)
            ))
        try:
            await client.run_until_disconnected()
        finally:
            for task in tasks:
                task.cancel()
    finally:
        await client.disconnect()
        storage.close()


COMMANDS = {"run": cmd_run, "login": cmd_login, "channels": cmd_channels}


def main() -> None:
    parser = argparse.ArgumentParser(prog="tg_parser", description=__doc__.strip().splitlines()[0])
    parser.add_argument("command", nargs="?", default="run", choices=COMMANDS)
    args = parser.parse_args()

    try:
        cfg = load_config()
    except ConfigError as e:
        sys.exit(f"ошибка конфигурации: {e}")

    logging.basicConfig(
        level=cfg.log_level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("telethon").setLevel(max(logging.WARNING, logging.getLevelName(cfg.log_level)))

    try:
        asyncio.run(COMMANDS[args.command](cfg))
    except LookupError as e:
        sys.exit(f"ошибка: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
