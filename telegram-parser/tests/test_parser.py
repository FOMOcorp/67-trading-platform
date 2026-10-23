import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from telethon import connection, utils
from telethon.tl.types import (
    Channel,
    Chat,
    ChatPhotoEmpty,
    DialogFilter,
    DialogFilterDefault,
    InputPeerChannel,
    InputPeerChat,
    InputPeerSelf,
    InputPeerUser,
    Message,
    MessageMediaPhoto,
    PeerChannel,
    PeerUser,
    TextWithEntities,
    User,
)

from tg_parser.config import ConfigError, load_config
from tg_parser.folder import chat_kind, dialogs_in_folder, find_folder, targets_in_folder
from tg_parser.history import history_cutoff, is_pending, load_batch
from tg_parser.proxy import describe_proxy, parse_proxy
from tg_parser.storage import Storage, message_row


def make_channel(channel_id, broadcast=True, title="chan", username=None):
    return Channel(
        id=channel_id,
        title=title,
        photo=ChatPhotoEmpty(),
        date=datetime(2026, 1, 1, tzinfo=timezone.utc),
        broadcast=broadcast,
        megagroup=not broadcast,
        username=username,
        access_hash=1,
    )


def peer_id(channel_id):
    return utils.get_peer_id(PeerChannel(channel_id))


def dialog(entity, archived=False):
    return SimpleNamespace(id=peer_id(entity.id), entity=entity, archived=archived)


def make_filter(title="crypto", include=(), exclude=(), **flags):
    return DialogFilter(
        id=2,
        title=TextWithEntities(text=title, entities=[]),
        pinned_peers=[],
        include_peers=[InputPeerChannel(i, 1) for i in include],
        exclude_peers=[InputPeerChannel(i, 1) for i in exclude],
        **flags,
    )


# --- папка ---


def test_find_folder_case_insensitive_and_skips_default():
    crypto = make_filter("Crypto")
    assert find_folder([DialogFilterDefault(), make_filter("news"), crypto], "crypto") is crypto
    assert find_folder([DialogFilterDefault()], "crypto") is None


def test_targets_in_folder_explicit_include():
    btc, eth, group, other = make_channel(1), make_channel(2), make_channel(3, broadcast=False), make_channel(4)
    f = make_filter(include=[1, 2, 3])
    result = targets_in_folder(f, [dialog(btc), dialog(eth), dialog(group), dialog(other)])
    assert set(result) == {peer_id(1), peer_id(2), peer_id(3)}


def test_targets_in_folder_takes_groups_skips_users_and_saved_messages():
    btc = make_channel(1)
    group = make_channel(3, broadcast=False)
    f = make_filter(include=[1, 3])
    f.include_peers += [InputPeerUser(5, 1), InputPeerChat(7), InputPeerSelf()]
    f.pinned_peers = [InputPeerSelf()]
    user = SimpleNamespace(id=5, entity=User(id=5), archived=False)
    chat = SimpleNamespace(id=-7, entity=Chat(id=7, title="g", photo=ChatPhotoEmpty(), participants_count=2, date=None, version=1), archived=False)
    result = targets_in_folder(f, [dialog(btc), dialog(group), user, chat])
    assert set(result) == {peer_id(1), peer_id(3), -7}


def test_targets_in_folder_all_broadcasts_with_excludes():
    a, b, c = make_channel(1), make_channel(2), make_channel(3)
    f = make_filter(exclude=[2], broadcasts=True, exclude_archived=True)
    result = targets_in_folder(f, [dialog(a), dialog(b), dialog(c, archived=True)])
    assert set(result) == {peer_id(1)}


# --- прокси ---


def test_parse_proxy_empty():
    assert parse_proxy(None) == {}
    assert parse_proxy("") == {}


def test_parse_proxy_socks5_with_auth():
    kwargs = parse_proxy("socks5h://us%40er:p%3Ass@10.0.0.1:1080")
    assert kwargs == {
        "proxy": {
            "proxy_type": "socks5",
            "addr": "10.0.0.1",
            "port": 1080,
            "rdns": True,
            "username": "us@er",
            "password": "p:ss",
        }
    }


def test_parse_proxy_http_without_auth():
    assert parse_proxy("http://proxy:3128")["proxy"] == {
        "proxy_type": "http",
        "addr": "proxy",
        "port": 3128,
        "rdns": True,
    }


def test_parse_proxy_mtproto():
    kwargs = parse_proxy("mtproto://ee00ff@mt.example.com:443")
    assert kwargs["connection"] is connection.ConnectionTcpMTProxyRandomizedIntermediate
    assert kwargs["proxy"] == ("mt.example.com", 443, "ee00ff")


@pytest.mark.parametrize("url", ["ftp://host:21", "socks5://host", "mtproto://host:443"])
def test_parse_proxy_invalid(url):
    with pytest.raises(ValueError):
        parse_proxy(url)


def test_describe_proxy_hides_credentials():
    assert describe_proxy("socks5://user:secret@host:1080") == "socks5://host:1080"


# --- конфиг ---


def test_load_config_defaults():
    cfg = load_config({"TG_API_ID": "123", "TG_API_HASH": "abc"})
    assert cfg.api_id == 123
    assert cfg.folder == "crypto"
    assert cfg.proxy_url is None
    assert str(cfg.db_path) == "data/messages.db"


@pytest.mark.parametrize("env", [{}, {"TG_API_ID": "123"}, {"TG_API_ID": "x", "TG_API_HASH": "abc"}])
def test_load_config_errors(env):
    with pytest.raises(ConfigError):
        load_config(env)


# --- хранилище ---


def make_message(msg_id, text="hello", media=None, from_id=None):
    return Message(
        id=msg_id,
        peer_id=PeerChannel(1),
        from_id=from_id,
        date=datetime(2026, 10, 23, 15, 0, tzinfo=timezone(timedelta(hours=3))),
        message=text,
        media=media,
        views=10,
        forwards=2,
    )


def test_storage_saves_once_and_tracks_last_id(tmp_path):
    storage = Storage(tmp_path / "sub" / "db.sqlite")
    channel = make_channel(1, title="BTC news", username="btcnews")

    row = message_row(make_message(5), channel)
    assert row["chat_id"] == peer_id(1)
    assert row["chat_type"] == "channel"
    assert row["date"] == "2026-10-23T12:00:00+00:00"
    assert row["chat_username"] == "btcnews"

    assert storage.save(row) is True
    assert storage.save(row) is False
    storage.save(message_row(make_message(7, text="", media=MessageMediaPhoto()), channel))

    assert storage.last_message_id(peer_id(1)) == 7
    assert storage.last_message_id(peer_id(2)) is None
    text, media = storage.conn.execute(
        "SELECT text, media_type FROM messages WHERE message_id = 7"
    ).fetchone()
    assert (text, media) == (None, "MessageMediaPhoto")


def test_dialogs_in_folder_lists_all_kinds_by_flags():
    me = SimpleNamespace(id=10, entity=User(id=10, is_self=True), archived=False, name="me")
    contact = SimpleNamespace(id=11, entity=User(id=11, contact=True), archived=False)
    stranger = SimpleNamespace(id=12, entity=User(id=12), archived=False)
    bot = SimpleNamespace(id=13, entity=User(id=13, bot=True), archived=False)
    group = dialog(make_channel(3, broadcast=False))
    channel = dialog(make_channel(1))
    f = make_filter(groups=True, contacts=True)
    f.include_peers.append(InputPeerSelf())
    result = dialogs_in_folder(f, [me, contact, stranger, bot, group, channel])
    assert result == [me, contact, group]
    assert [chat_kind(d.entity) for d in result] == ["saved", "user", "supergroup"]
    assert set(targets_in_folder(f, [me, contact, stranger, bot, group, channel])) == {group.id}


def test_message_row_group_sender():
    group = make_channel(1, broadcast=False, title="BTC chat")
    message = make_message(3, from_id=PeerUser(42))
    message._sender = User(id=42, first_name="Vasya", last_name="Pupkin", username="vasya")
    row = message_row(message, group)
    assert row["chat_type"] == "supergroup"
    assert (row["sender_id"], row["sender_username"], row["sender_name"]) == (42, "vasya", "Vasya Pupkin")


def test_storage_migrates_channels_only_schema(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE messages (
            channel_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
            channel_title TEXT, channel_username TEXT, date TEXT NOT NULL, text TEXT,
            media_type TEXT, grouped_id INTEGER, reply_to_msg_id INTEGER,
            views INTEGER, forwards INTEGER, received_at TEXT NOT NULL,
            PRIMARY KEY (channel_id, message_id)
        );
        INSERT INTO messages VALUES (-1001, 9, 'old', NULL, '2026-01-01', 'hi', NULL, NULL, NULL, 1, 0, '2026-01-01');
        """
    )
    conn.commit()
    conn.close()

    storage = Storage(path)
    assert storage.last_message_id(-1001) == 9
    assert storage.conn.execute("SELECT chat_type, chat_title, sender_id FROM messages").fetchone() == ("channel", "old", None)
    assert storage.save(message_row(make_message(10), make_channel(1))) is True
    storage.close()
    Storage(path).close()  # повторный запуск на уже мигрированной базе


# --- история ---


class FakeClient:
    """get_messages как у Telethon: от новых к старым, строго старше offset_id."""

    def __init__(self, messages):
        self.messages = sorted(messages, key=lambda m: m.id, reverse=True)
        self.calls = []

    async def get_messages(self, chat, limit, offset_id):
        self.calls.append(offset_id)
        older = [m for m in self.messages if not offset_id or m.id < offset_id]
        return older[:limit]


def dated_message(msg_id, days_ago, now):
    m = make_message(msg_id)
    m.date = now - timedelta(days=days_ago)
    return m


def test_history_cutoff():
    now = datetime(2026, 10, 23, tzinfo=timezone.utc)
    assert history_cutoff(-1, now) is None
    assert history_cutoff(10, now) == datetime(2026, 10, 13, tzinfo=timezone.utc)


def test_load_batch_pages_back_until_chat_start(tmp_path):
    now = datetime.now(timezone.utc)
    storage = Storage(tmp_path / "db.sqlite")
    chat = make_channel(1)
    client = FakeClient([dated_message(i, 250 - i, now) for i in range(1, 251)])
    # live-сообщение уже есть: история должна пойти от него вглубь
    storage.save(message_row(dated_message(250, 0, now), chat))

    async def run():
        results = []
        while is_pending(storage.history_state(peer_id(1)), None):
            results.append(await load_batch(client, storage, peer_id(1), chat, None))
        return results

    assert asyncio.run(run()) == [100, 100, 49]
    assert client.calls == [250, 150, 50]
    assert storage.conn.execute("SELECT count(*) FROM messages").fetchone() == (250,)
    assert storage.history_state(peer_id(1))["exhausted"] is True


def test_load_batch_stops_at_cutoff(tmp_path):
    now = datetime.now(timezone.utc)
    storage = Storage(tmp_path / "db.sqlite")
    chat = make_channel(1)
    client = FakeClient([dated_message(i, 300 - i, now) for i in range(1, 301)])
    cutoff = history_cutoff(30)

    saved = asyncio.run(load_batch(client, storage, peer_id(1), chat, cutoff))
    assert saved == 30  # сообщения за последние 30 дней (ids 271..300)
    assert not is_pending(storage.history_state(peer_id(1)), cutoff)
    assert is_pending(storage.history_state(peer_id(1)), history_cutoff(200))  # увеличили глубину — продолжит


# --- метрики ---


def test_record_saved_counts_and_tracks_live():
    from prometheus_client import REGISTRY

    from tg_parser import metrics

    def value(name, **labels):
        return REGISTRY.get_sample_value(name, labels) or 0

    before_live = value("tg_messages_saved_total", chat_type="channel", source="live")
    before_hist = value("tg_messages_saved_total", chat_type="supergroup", source="history")
    now = datetime.now(timezone.utc)
    metrics.record_saved({"chat_type": "channel", "date": (now - timedelta(seconds=3)).isoformat()}, "live")
    metrics.record_saved({"chat_type": "supergroup", "date": now.isoformat()}, "history")

    assert value("tg_messages_saved_total", chat_type="channel", source="live") == before_live + 1
    assert value("tg_messages_saved_total", chat_type="supergroup", source="history") == before_hist + 1
    assert value("tg_last_message_timestamp_seconds") >= now.timestamp()
    assert value("tg_delivery_delay_seconds_bucket", le="5.0") >= 1
