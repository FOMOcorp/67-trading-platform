"""Хранение сообщений в локальной SQLite."""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from telethon import utils

from .folder import chat_kind

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    chat_id          INTEGER NOT NULL,
    message_id       INTEGER NOT NULL,
    chat_type        TEXT,
    chat_title       TEXT,
    chat_username    TEXT,
    sender_id        INTEGER,
    sender_username  TEXT,
    sender_name      TEXT,
    date             TEXT NOT NULL,
    text             TEXT,
    media_type       TEXT,
    grouped_id       INTEGER,
    reply_to_msg_id  INTEGER,
    views            INTEGER,
    forwards         INTEGER,
    received_at      TEXT NOT NULL,
    PRIMARY KEY (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_date ON messages (date);

-- докуда догружена история: cursor_id — самое старое загруженное сообщение
CREATE TABLE IF NOT EXISTS history_state (
    chat_id      INTEGER PRIMARY KEY,
    cursor_id    INTEGER NOT NULL,
    cursor_date  TEXT,
    exhausted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
"""

COLUMNS = (
    "chat_id",
    "message_id",
    "chat_type",
    "chat_title",
    "chat_username",
    "sender_id",
    "sender_username",
    "sender_name",
    "date",
    "text",
    "media_type",
    "grouped_id",
    "reply_to_msg_id",
    "views",
    "forwards",
    "received_at",
)

# миграция базы первой версии, где парсились только каналы
RENAMED_COLUMNS = {
    "channel_id": "chat_id",
    "channel_title": "chat_title",
    "channel_username": "chat_username",
}
ADDED_COLUMNS = {
    "chat_type": "TEXT",
    "sender_id": "INTEGER",
    "sender_username": "TEXT",
    "sender_name": "TEXT",
}


def message_row(message, chat) -> dict:
    """Строка таблицы messages из telethon Message и сущности чата."""
    reply_to = getattr(message, "reply_to", None)
    sender = message.sender
    return {
        "chat_id": message.chat_id,
        "message_id": message.id,
        "chat_type": chat_kind(chat) if chat is not None else None,
        "chat_title": getattr(chat, "title", None),
        "chat_username": getattr(chat, "username", None),
        "sender_id": message.sender_id,
        "sender_username": getattr(sender, "username", None),
        "sender_name": (utils.get_display_name(sender) or None) if sender else None,
        "date": message.date.astimezone(timezone.utc).isoformat(),
        "text": message.message or None,
        "media_type": type(message.media).__name__ if message.media else None,
        "grouped_id": message.grouped_id,
        "reply_to_msg_id": getattr(reply_to, "reply_to_msg_id", None),
        "views": message.views,
        "forwards": message.forwards,
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._migrate()
        self.conn.executescript(SCHEMA)

    def _migrate(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(messages)")}
        if not columns or "channel_id" not in columns:
            return
        with self.conn:
            for old, new in RENAMED_COLUMNS.items():
                self.conn.execute(f"ALTER TABLE messages RENAME COLUMN {old} TO {new}")
            for name, decl in ADDED_COLUMNS.items():
                if name not in columns:
                    self.conn.execute(f"ALTER TABLE messages ADD COLUMN {name} {decl}")
            self.conn.execute("UPDATE messages SET chat_type = 'channel' WHERE chat_type IS NULL")

    def save(self, row: dict) -> bool:
        """Сохраняет сообщение. False, если оно уже было в базе."""
        placeholders = ", ".join("?" for _ in COLUMNS)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO messages ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            [row[c] for c in COLUMNS],
        )
        self.conn.commit()
        return cur.rowcount == 1

    def last_message_id(self, chat_id: int) -> int | None:
        (value,) = self.conn.execute(
            "SELECT MAX(message_id) FROM messages WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        return value

    def history_state(self, chat_id: int) -> dict:
        """Состояние загрузки истории. Если его нет — начинаем от самого старого
        сохранённого сообщения, а для пустого чата — с самого нового."""
        row = self.conn.execute(
            "SELECT cursor_id, cursor_date, exhausted FROM history_state WHERE chat_id = ?", (chat_id,)
        ).fetchone()
        if row is None:
            row = self.conn.execute(
                "SELECT message_id, date, 0 FROM messages WHERE chat_id = ? ORDER BY message_id LIMIT 1",
                (chat_id,),
            ).fetchone() or (0, None, 0)
        cursor_id, cursor_date, exhausted = row
        return {"cursor_id": cursor_id, "cursor_date": cursor_date, "exhausted": bool(exhausted)}

    def set_history_state(self, chat_id: int, state: dict) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO history_state (chat_id, cursor_id, cursor_date, exhausted, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (chat_id, state["cursor_id"], state["cursor_date"], int(state["exhausted"]),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def close(self):
        self.conn.close()
