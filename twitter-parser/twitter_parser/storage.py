"""Хранение твитов в локальной SQLite."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets (
    tweet_id            INTEGER PRIMARY KEY,
    account             TEXT NOT NULL,
    author_id           TEXT,
    author_username     TEXT,
    author_name         TEXT,
    created_at          TEXT NOT NULL,
    text                TEXT,
    lang                TEXT,
    url                 TEXT,
    is_reply            INTEGER NOT NULL,
    in_reply_to_id      TEXT,
    conversation_id     TEXT,
    quoted_tweet_id     TEXT,
    retweeted_tweet_id  TEXT,
    like_count          INTEGER,
    retweet_count       INTEGER,
    reply_count         INTEGER,
    quote_count         INTEGER,
    view_count          INTEGER,
    bookmark_count      INTEGER,
    raw                 TEXT NOT NULL,
    received_at         TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tweets_account_created ON tweets (account, created_at);
CREATE INDEX IF NOT EXISTS idx_tweets_created ON tweets (created_at);

-- докуда догружена история: cursor_time — время самого старого загруженного твита
CREATE TABLE IF NOT EXISTS history_state (
    account      TEXT PRIMARY KEY,
    cursor_time  TEXT,
    empty_pages  INTEGER NOT NULL DEFAULT 0,
    exhausted    INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
"""

COLUMNS = (
    "tweet_id",
    "account",
    "author_id",
    "author_username",
    "author_name",
    "created_at",
    "text",
    "lang",
    "url",
    "is_reply",
    "in_reply_to_id",
    "conversation_id",
    "quoted_tweet_id",
    "retweeted_tweet_id",
    "like_count",
    "retweet_count",
    "reply_count",
    "quote_count",
    "view_count",
    "bookmark_count",
    "raw",
    "received_at",
)


def parse_created_at(value: str) -> datetime:
    """createdAt от twitterapi.io: «Tue Dec 10 07:00:30 +0000 2024»; ISO тоже принимаем."""
    try:
        date = datetime.strptime(value, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if date.tzinfo is None:
        raise ValueError(f"нет часового пояса в дате {value!r}")
    return date.astimezone(timezone.utc)


def _nested_id(tweet: dict, key: str) -> str | None:
    nested = tweet.get(key)
    return str(nested["id"]) if isinstance(nested, dict) and nested.get("id") else None


def _str(value) -> str | None:
    return str(value) if value not in (None, "") else None


def tweet_row(tweet: dict, account: str) -> dict:
    """Строка таблицы tweets из твита в формате twitterapi.io."""
    author = tweet.get("author") or {}
    return {
        "tweet_id": int(tweet["id"]),
        "account": account,
        "author_id": _str(author.get("id")),
        "author_username": author.get("userName"),
        "author_name": author.get("name"),
        "created_at": parse_created_at(tweet["createdAt"]).isoformat(),
        "text": tweet.get("text"),
        "lang": tweet.get("lang"),
        "url": tweet.get("url") or tweet.get("twitterUrl"),
        "is_reply": int(bool(tweet.get("isReply"))),
        "in_reply_to_id": _str(tweet.get("inReplyToId")),
        "conversation_id": _str(tweet.get("conversationId")),
        "quoted_tweet_id": _nested_id(tweet, "quoted_tweet"),
        "retweeted_tweet_id": _nested_id(tweet, "retweeted_tweet"),
        "like_count": tweet.get("likeCount"),
        "retweet_count": tweet.get("retweetCount"),
        "reply_count": tweet.get("replyCount"),
        "quote_count": tweet.get("quoteCount"),
        "view_count": tweet.get("viewCount"),
        "bookmark_count": tweet.get("bookmarkCount"),
        # полный ответ API: медиа, ссылки, хэштеги и вложенные твиты разберём при анализе
        "raw": json.dumps(tweet, ensure_ascii=False),
        "received_at": datetime.now(timezone.utc).isoformat(),
    }


class Storage:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(SCHEMA)

    def save(self, row: dict) -> bool:
        """Сохраняет твит. False, если он уже был в базе."""
        placeholders = ", ".join("?" for _ in COLUMNS)
        cur = self.conn.execute(
            f"INSERT OR IGNORE INTO tweets ({', '.join(COLUMNS)}) VALUES ({placeholders})",
            [row[c] for c in COLUMNS],
        )
        self.conn.commit()
        return cur.rowcount == 1

    def newest_created_at(self, account: str) -> datetime | None:
        (value,) = self.conn.execute(
            "SELECT MAX(created_at) FROM tweets WHERE account = ?", (account,)
        ).fetchone()
        return datetime.fromisoformat(value) if value else None

    def history_state(self, account: str) -> dict:
        """Состояние загрузки истории. Если его нет — начинаем от самого старого
        сохранённого твита, а для пустого аккаунта — с самого нового."""
        row = self.conn.execute(
            "SELECT cursor_time, empty_pages, exhausted FROM history_state WHERE account = ?", (account,)
        ).fetchone()
        if row is None:
            (oldest,) = self.conn.execute(
                "SELECT MIN(created_at) FROM tweets WHERE account = ?", (account,)
            ).fetchone()
            row = (oldest, 0, 0)
        cursor_time, empty_pages, exhausted = row
        return {
            "cursor_time": datetime.fromisoformat(cursor_time) if cursor_time else None,
            "empty_pages": empty_pages,
            "exhausted": bool(exhausted),
        }

    def set_history_state(self, account: str, state: dict) -> None:
        cursor_time = state["cursor_time"].isoformat() if state["cursor_time"] else None
        self.conn.execute(
            "INSERT OR REPLACE INTO history_state (account, cursor_time, empty_pages, exhausted, updated_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (account, cursor_time, state["empty_pages"], int(state["exhausted"]),
             datetime.now(timezone.utc).isoformat()),
        )
        self.conn.commit()

    def count(self, account: str) -> int:
        (value,) = self.conn.execute("SELECT COUNT(*) FROM tweets WHERE account = ?", (account,)).fetchone()
        return value

    def close(self):
        self.conn.close()
