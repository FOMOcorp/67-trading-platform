"""Метрики Prometheus. Отдаются на http://<host>:METRICS_PORT/metrics."""

from datetime import datetime, timezone
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram, start_http_server

TWEETS_SAVED = Counter(
    "tw_tweets_saved_total",
    "Сохранённые твиты. source: live (новые), history",
    ["source"],
)
LAST_TWEET = Gauge(
    "tw_last_tweet_timestamp_seconds",
    "Время сохранения последнего live-твита (unix)",
)
DELIVERY_DELAY = Histogram(
    "tw_delivery_delay_seconds",
    "Задержка между публикацией live-твита и его сохранением",
    buckets=(30, 60, 120, 300, 600, 1800, 3600),
)
LAST_POLL = Gauge("tw_last_poll_timestamp_seconds", "Время завершения последнего опроса новых твитов (unix)")
TRACKED_ACCOUNTS = Gauge("tw_tracked_accounts", "Аккаунты, которые парсятся")
HISTORY_PENDING = Gauge("tw_history_pending_accounts", "Аккаунты, у которых ещё догружается история")
HISTORY_OLDEST = Gauge(
    "tw_history_oldest_tweet_timestamp_seconds",
    "Докуда догружена история аккаунта (unix-время самого старого твита)",
    ["account"],
)
API_REQUESTS = Counter("tw_api_requests_total", "Запросы к twitterapi.io по HTTP-статусу", ["status"])
ERRORS = Counter("tw_errors_total", "Ошибки по этапам", ["stage"])
DB_SIZE = Gauge("tw_db_size_bytes", "Размер файла SQLite вместе с WAL")


def start(port: int, db_path: Path) -> None:
    start_http_server(port)
    DB_SIZE.set_function(lambda: _size(db_path) + _size(db_path.with_name(db_path.name + "-wal")))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def record_saved(row: dict, source: str) -> None:
    TWEETS_SAVED.labels(source=source).inc()
    if source == "live":
        now = datetime.now(timezone.utc)
        LAST_TWEET.set(now.timestamp())
        DELIVERY_DELAY.observe(max(0.0, (now - datetime.fromisoformat(row["created_at"])).total_seconds()))
