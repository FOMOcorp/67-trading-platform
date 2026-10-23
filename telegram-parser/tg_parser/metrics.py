"""Метрики Prometheus. Отдаются на http://<host>:METRICS_PORT/metrics."""

from datetime import datetime, timezone
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram, start_http_server

MESSAGES_SAVED = Counter(
    "tg_messages_saved_total",
    "Сохранённые сообщения. source: live, catchup (после рестарта), history",
    ["chat_type", "source"],
)
LAST_MESSAGE = Gauge(
    "tg_last_message_timestamp_seconds",
    "Время сохранения последнего live-сообщения (unix)",
)
DELIVERY_DELAY = Histogram(
    "tg_delivery_delay_seconds",
    "Задержка между публикацией live-сообщения и его сохранением",
    buckets=(0.5, 1, 2, 5, 10, 30, 60, 300),
)
TRACKED_CHATS = Gauge("tg_tracked_chats", "Чаты из папки, которые парсятся", ["kind"])
CONNECTED = Gauge("tg_connected", "1, если есть соединение с Telegram")
HISTORY_PENDING = Gauge("tg_history_pending_chats", "Чаты, у которых ещё догружается история")
HISTORY_OLDEST = Gauge(
    "tg_history_oldest_message_timestamp_seconds",
    "Докуда догружена история чата (unix-время самого старого сообщения)",
    ["chat"],
)
FLOOD_WAITS = Counter("tg_flood_waits_total", "Ответы FloodWait от Telegram")
FLOOD_WAIT_SECONDS = Counter("tg_flood_wait_seconds_total", "Сколько секунд суммарно ждали по FloodWait")
ERRORS = Counter("tg_errors_total", "Ошибки по этапам", ["stage"])
DB_SIZE = Gauge("tg_db_size_bytes", "Размер файла SQLite вместе с WAL")


def start(port: int, db_path: Path, is_connected) -> None:
    start_http_server(port)
    CONNECTED.set_function(lambda: float(is_connected()))
    DB_SIZE.set_function(lambda: _size(db_path) + _size(db_path.with_name(db_path.name + "-wal")))


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def record_saved(row: dict, source: str) -> None:
    MESSAGES_SAVED.labels(chat_type=row["chat_type"] or "unknown", source=source).inc()
    if source == "live":
        now = datetime.now(timezone.utc)
        LAST_MESSAGE.set(now.timestamp())
        DELIVERY_DELAY.observe(max(0.0, (now - datetime.fromisoformat(row["date"])).total_seconds()))
