"""Фоновая загрузка старых сообщений: медленно, по одному запросу за раз.

Чаты обходятся по кругу, за проход из каждого берётся одна пачка (до 100
сообщений, это один запрос GetHistory) от самого старого сохранённого
сообщения вглубь. Между запросами пауза со случайным разбросом.
Позиция хранится в таблице history_state, поэтому после перезапуска
загрузка продолжается с того же места.
"""

import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

from telethon.errors import FloodWaitError
from telethon.tl.types import MessageService

from . import metrics
from .storage import Storage, message_row

log = logging.getLogger("tg_parser.history")

BATCH_SIZE = 100  # максимум сообщений за один запрос GetHistory


def history_cutoff(days: int, now: datetime | None = None) -> datetime | None:
    """Граница по дате: None — вся история."""
    if days < 0:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def is_pending(state: dict, cutoff: datetime | None) -> bool:
    """Есть ли ещё что грузить для чата с таким состоянием."""
    if state["exhausted"]:
        return False
    if cutoff is None or state["cursor_date"] is None:
        return True
    return datetime.fromisoformat(state["cursor_date"]) > cutoff


async def load_batch(client, storage: Storage, chat_id: int, chat, cutoff: datetime | None) -> int:
    """Одна пачка истории старше курсора. Возвращает число новых сообщений."""
    state = storage.history_state(chat_id)
    messages = await client.get_messages(chat, limit=BATCH_SIZE, offset_id=state["cursor_id"])

    saved = 0
    for message in messages:
        if cutoff is not None and message.date < cutoff:
            break
        if not isinstance(message, MessageService):
            row = message_row(message, chat)
            if storage.save(row):
                metrics.record_saved(row, "history")
                saved += 1

    if messages:
        oldest = messages[-1]
        state["cursor_id"] = oldest.id
        state["cursor_date"] = oldest.date.astimezone(timezone.utc).isoformat()
        metrics.HISTORY_OLDEST.labels(chat=getattr(chat, "title", str(chat_id))).set(oldest.date.timestamp())
    # меньше полной пачки — дошли до начала чата
    state["exhausted"] = len(messages) < BATCH_SIZE
    storage.set_history_state(chat_id, state)
    return saved


async def history_loop(client, storage: Storage, chats: dict, days: int, delay: float) -> None:
    """Бесконечно догружает историю чатов из chats (словарь обновляется снаружи)."""
    cutoff = history_cutoff(days)
    log.info("загрузка истории: %s, пауза между запросами ~%s с",
             "вся" if cutoff is None else f"с {cutoff:%Y-%m-%d}", delay)
    while True:
        pending = [
            (chat_id, chat)
            for chat_id, chat in list(chats.items())
            if is_pending(storage.history_state(chat_id), cutoff)
        ]
        metrics.HISTORY_PENDING.set(len(pending))
        if not pending:
            await asyncio.sleep(60)  # ждём новых чатов в папке
            continue

        for chat_id, chat in pending:
            if chat_id not in chats:
                continue  # чат убрали из папки
            try:
                saved = await load_batch(client, storage, chat_id, chat, cutoff)
                state = storage.history_state(chat_id)
                log.info("[%s] история: +%d, дошли до %s%s",
                         getattr(chat, "title", chat_id), saved,
                         (state["cursor_date"] or "-")[:10],
                         ", готово" if not is_pending(state, cutoff) else "")
            except FloodWaitError as e:
                metrics.FLOOD_WAITS.inc()
                metrics.FLOOD_WAIT_SECONDS.inc(e.seconds)
                log.warning("FloodWait от Telegram: пауза %d с", e.seconds)
                await asyncio.sleep(e.seconds + 5)
            except Exception:
                metrics.ERRORS.labels(stage="history").inc()
                log.exception("[%s] не удалось загрузить историю, повторю позже",
                              getattr(chat, "title", chat_id))
            await asyncio.sleep(delay * random.uniform(1, 1.5))
