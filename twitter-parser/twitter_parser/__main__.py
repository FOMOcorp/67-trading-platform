"""
Парсер постов Twitter/X-аккаунтов через twitterapi.io с сохранением в SQLite.

Команды:
    python -m twitter_parser         # новые твиты каждые POLL_SECONDS + фоновая загрузка истории (по умолчанию)
    python -m twitter_parser stats   # сколько твитов сохранено и докуда догружена история
"""

import argparse
import logging
import random
import sys
import time

from . import metrics
from .api import ApiError, TwitterApi
from .collector import history_cutoff, is_pending, load_history_page, poll_new
from .config import Config, ConfigError, load_config
from .storage import Storage

log = logging.getLogger("twitter_parser")


def poll_all(api: TwitterApi, storage: Storage, cfg: Config) -> None:
    for account in cfg.accounts:
        try:
            poll_new(api, storage, account, cfg.include_replies, cfg.poll_max_pages)
        except ApiError:
            raise
        except Exception:
            metrics.ERRORS.labels(stage="poll").inc()
            log.exception("[@%s] не удалось получить новые твиты, повторю в следующем опросе", account)
    metrics.LAST_POLL.set(time.time())


def history_step(api: TwitterApi, storage: Storage, cfg: Config, cutoff, turn: int) -> bool:
    """Одна страница истории для очередного аккаунта по кругу. False — грузить нечего."""
    pending = [a for a in cfg.accounts if is_pending(storage.history_state(a), cutoff)]
    metrics.HISTORY_PENDING.set(len(pending))
    if not pending:
        return False
    account = pending[turn % len(pending)]
    try:
        saved = load_history_page(api, storage, account, cfg.include_replies, cutoff)
        state = storage.history_state(account)
        log.info("[@%s] история: +%d, дошли до %s%s", account, saved,
                 f"{state['cursor_time']:%Y-%m-%d}" if state["cursor_time"] else "-",
                 ", готово" if not is_pending(state, cutoff) else "")
    except ApiError:
        raise
    except Exception:
        metrics.ERRORS.labels(stage="history").inc()
        log.exception("[@%s] не удалось загрузить историю, повторю позже", account)
    return True


def cmd_run(cfg: Config) -> None:
    storage = Storage(cfg.db_path)
    api = TwitterApi(cfg.api_key, cfg.proxy_url)
    if cfg.metrics_port:
        metrics.start(cfg.metrics_port, cfg.db_path)
        log.info("метрики: http://0.0.0.0:%d/metrics", cfg.metrics_port)
    metrics.TRACKED_ACCOUNTS.set(len(cfg.accounts))

    cutoff = history_cutoff(cfg.history_days) if cfg.history_days else None
    log.info("аккаунты: %s; опрос каждые %d с; история: %s; база: %s",
             ", ".join(f"@{a}" for a in cfg.accounts), cfg.poll_seconds,
             "выключена" if not cfg.history_days else "вся" if cutoff is None else f"с {cutoff:%Y-%m-%d}",
             cfg.db_path)
    next_poll, turn = 0.0, 0
    try:
        while True:
            if time.monotonic() >= next_poll:
                # интервал отсчитывается от начала опроса
                next_poll = time.monotonic() + cfg.poll_seconds
                poll_all(api, storage, cfg)
            if cfg.history_days and history_step(api, storage, cfg, cutoff, turn):
                turn += 1
                time.sleep(cfg.history_delay_seconds * random.uniform(1, 1.5))
            else:
                time.sleep(max(0.0, next_poll - time.monotonic()))
    finally:
        api.close()
        storage.close()


def cmd_stats(cfg: Config) -> None:
    storage = Storage(cfg.db_path)
    cutoff = history_cutoff(cfg.history_days) if cfg.history_days else None
    try:
        print(f"База {cfg.db_path}")
        for account in cfg.accounts:
            state = storage.history_state(account)
            oldest = f"{state['cursor_time']:%Y-%m-%d}" if state["cursor_time"] else "-"
            newest = storage.newest_created_at(account)
            status = "догружается" if cfg.history_days and is_pending(state, cutoff) else "готово"
            print(f"  @{account:<16} {storage.count(account):>8} твитов"
                  f"  с {oldest} по {f'{newest:%Y-%m-%d}' if newest else '-'}  история: {status}")
    finally:
        storage.close()


COMMANDS = {"run": cmd_run, "stats": cmd_stats}


def main() -> None:
    parser = argparse.ArgumentParser(prog="twitter_parser", description=__doc__.strip().splitlines()[0])
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
    # httpx на INFO пишет каждый запрос вместе с query
    logging.getLogger("httpx").setLevel(max(logging.WARNING, logging.getLevelName(cfg.log_level)))

    try:
        COMMANDS[args.command](cfg)
    except ApiError as e:
        sys.exit(f"ошибка twitterapi.io: {e}")
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
