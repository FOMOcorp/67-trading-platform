"""Загрузка архива и проверки новых публикаций каждые 5 минут."""

import argparse
import logging
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.error import HTTPError, URLError
from xml.etree.ElementTree import ParseError

from source import Source, parse_article
from storage import CsvStorage

ROOT = Path(__file__).resolve().parent
logger = logging.getLogger(__name__)


def collect(storage, history, limit, reverse=False):
    latest = storage.latest_published_at() if history and reverse else None
    start_month = latest.strftime("%Y-%m") if latest else None
    if history and reverse:
        logger.info("History: newest months first, starting from %s", start_month or "latest published sitemap")
    source = Source()
    saved = failed = 0
    for url in source.urls(history, reverse=reverse, start_month=start_month):
        if storage.contains(url):
            continue
        try:
            article = parse_article(source.get(url), url)
        except HTTPError as exc:
            if exc.code not in {404, 410}:
                raise
            logger.warning("%s: HTTP %s", url, exc.code)
            failed += 1
            continue
        except (URLError, TimeoutError) as exc:
            logger.warning("%s: %s", url, exc)
            failed += 1
            continue
        except ValueError as exc:
            logger.warning("%s: %s", url, exc)
            failed += 1
            continue
        saved += storage.save(article)
        logger.info("saved %s", url)
        if limit and saved >= limit:
            break
    logger.info("saved=%s, failed=%s", saved, failed)
    return failed


def retry_delay(value):
    try:
        return max(300, int(value))
    except (TypeError, ValueError):
        try:
            return max(300, (parsedate_to_datetime(value)
                             - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError, OverflowError):
            return 300


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="run",
                        choices=["run", "history", "once", "watch"],
                        help="run (default): load history, then watch every 5 minutes")
    parser.add_argument("--limit", type=int, default=0, help="New articles per pass; 0 = unlimited")
    parser.add_argument("--reverse", action="store_true",
                        help="history/run: visit months newest first, starting from latest saved publication")
    args = parser.parse_args()
    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.reverse and args.mode not in {"history", "run"}:
        parser.error("--reverse is available only for history and run")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # Для Postgres меняется эта строка; сборщику нужны только contains и save.
    storage = CsvStorage(ROOT / "data" / "articles.csv")
    had_errors = False
    history_pending = args.mode in {"run", "history"}
    while True:
        started = time.monotonic()
        retry_at = 0
        try:
            failed = collect(storage, history_pending, args.limit, reverse=args.reverse and history_pending)
            had_errors = had_errors or bool(failed)
            history_pending = False
        except HTTPError as exc:
            had_errors = True
            logger.error("HTTP %s at %s", exc.code, exc.url)
            if exc.code in {401, 403, 404, 410} or 300 <= exc.code < 400:
                logger.error("Collection stopped; source must be checked")
                return 1
            if exc.code in {429, 503}:
                retry_at = time.monotonic() + retry_delay(exc.headers.get("Retry-After"))
        except PermissionError as exc:
            logger.error("Collection stopped: %s", exc)
            return 1
        except (URLError, TimeoutError, OSError, ValueError, ParseError) as exc:
            had_errors = True
            logger.error("%s", exc)
        if args.mode in {"history", "once"}:
            return 1 if had_errors else 0
        # Retry-After отсчитывается от ответа, а обычный интервал — от начала цикла.
        wait = max(0, max(started + 300, retry_at) - time.monotonic())
        logger.info("Next cycle in %.0f seconds", wait)
        time.sleep(wait)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)
