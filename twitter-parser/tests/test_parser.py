import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from twitter_parser.api import ApiError, TwitterApi
from twitter_parser.collector import (
    EMPTY_PAGES_TO_EXHAUST,
    build_query,
    history_cutoff,
    is_pending,
    load_history_page,
    own_tweets,
    poll_new,
)
from twitter_parser.config import ConfigError, load_config, parse_accounts
from twitter_parser.storage import Storage, parse_created_at, tweet_row

BASE = datetime(2026, 10, 1, 12, 0, tzinfo=timezone.utc)


def created(dt):
    return dt.strftime("%a %b %d %H:%M:%S +0000 %Y")


def make_tweet(tweet_id, dt, author="vitalik", **extra):
    return {
        "id": str(tweet_id),
        "url": f"https://x.com/{author}/status/{tweet_id}",
        "text": f"tweet {tweet_id}",
        "createdAt": created(dt),
        "lang": "en",
        "isReply": False,
        "likeCount": 10,
        "retweetCount": 2,
        "replyCount": 1,
        "quoteCount": 0,
        "viewCount": 100,
        "bookmarkCount": 0,
        "author": {"id": "1", "userName": author, "name": author.title()},
        **extra,
    }


class FakeApi:
    """Отдаёт твиты аккаунта так же, как поиск: новые первыми, страницами по page_size."""

    def __init__(self, tweets, page_size=3):
        self.tweets = sorted(tweets, key=lambda t: parse_created_at(t["createdAt"]), reverse=True)
        self.page_size = page_size
        self.queries = []

    def search(self, query, cursor=""):
        self.queries.append((query, cursor))
        since = until = None
        for part in query.split():
            if part.startswith("since_time:"):
                since = int(part.split(":")[1])
            if part.startswith("until_time:"):
                until = int(part.split(":")[1])
        matched = [
            t for t in self.tweets
            if (since is None or parse_created_at(t["createdAt"]).timestamp() >= since)
            and (until is None or parse_created_at(t["createdAt"]).timestamp() <= until)
        ]
        start = int(cursor or 0)
        page = matched[start:start + self.page_size]
        has_next = start + self.page_size < len(matched)
        return {"tweets": page, "has_next_page": has_next,
                "next_cursor": str(start + self.page_size) if has_next else ""}


@pytest.fixture
def storage(tmp_path):
    s = Storage(tmp_path / "tweets.db")
    yield s
    s.close()


def ids(storage, account="vitalik"):
    rows = storage.conn.execute(
        "SELECT tweet_id FROM tweets WHERE account = ? ORDER BY tweet_id", (account,)
    ).fetchall()
    return [r[0] for r in rows]


# config

def test_parse_accounts_normalizes_and_dedups():
    assert parse_accounts("@VitalikButerin, cz_binance saylor,vitalikbuterin") == (
        "vitalikbuterin", "cz_binance", "saylor")


def test_parse_accounts_rejects_bad_names():
    with pytest.raises(ConfigError):
        parse_accounts("https://x.com/elonmusk")
    with pytest.raises(ConfigError):
        parse_accounts(" , ")


def test_load_config_defaults_and_required():
    cfg = load_config({"TWITTERAPI_IO_KEY": "k", "TW_ACCOUNTS": "a,b"})
    assert cfg.accounts == ("a", "b")
    assert cfg.poll_seconds == 300 and cfg.history_days == 365 and not cfg.include_replies
    with pytest.raises(ConfigError, match="TWITTERAPI_IO_KEY"):
        load_config({"TW_ACCOUNTS": "a"})
    with pytest.raises(ConfigError, match="POLL_SECONDS"):
        load_config({"TWITTERAPI_IO_KEY": "k", "TW_ACCOUNTS": "a", "POLL_SECONDS": "10"})
    with pytest.raises(ConfigError, match="INCLUDE_REPLIES"):
        load_config({"TWITTERAPI_IO_KEY": "k", "TW_ACCOUNTS": "a", "INCLUDE_REPLIES": "maybe"})


# storage

def test_parse_created_at_formats():
    assert parse_created_at("Tue Dec 10 07:00:30 +0000 2024") == datetime(2024, 12, 10, 7, 0, 30, tzinfo=timezone.utc)
    assert parse_created_at("2024-12-10T07:00:30Z") == datetime(2024, 12, 10, 7, 0, 30, tzinfo=timezone.utc)
    with pytest.raises(ValueError):
        parse_created_at("2024-12-10T07:00:30")


def test_tweet_row_fields():
    tweet = make_tweet(5, BASE, isReply=True, inReplyToId=4, conversationId=3,
                       quoted_tweet={"id": "2"}, retweeted_tweet=None)
    row = tweet_row(tweet, "vitalik")
    assert row["tweet_id"] == 5
    assert row["created_at"] == "2026-10-01T12:00:00+00:00"
    assert row["is_reply"] == 1 and row["in_reply_to_id"] == "4" and row["conversation_id"] == "3"
    assert row["quoted_tweet_id"] == "2" and row["retweeted_tweet_id"] is None
    assert row["author_username"] == "vitalik" and row["view_count"] == 100
    assert json.loads(row["raw"])["id"] == "5"


def test_save_is_idempotent(storage):
    row = tweet_row(make_tweet(1, BASE), "vitalik")
    assert storage.save(row)
    assert not storage.save(row)
    assert storage.count("vitalik") == 1


def test_history_state_defaults_to_oldest_saved(storage):
    assert storage.history_state("vitalik") == {"cursor_time": None, "empty_pages": 0, "exhausted": False}
    storage.save(tweet_row(make_tweet(2, BASE), "vitalik"))
    storage.save(tweet_row(make_tweet(1, BASE - timedelta(hours=1)), "vitalik"))
    assert storage.history_state("vitalik")["cursor_time"] == BASE - timedelta(hours=1)
    state = {"cursor_time": BASE - timedelta(days=3), "empty_pages": 1, "exhausted": False}
    storage.set_history_state("vitalik", state)
    assert storage.history_state("vitalik") == state


# collector

def test_build_query():
    assert build_query("vitalik", False) == "from:vitalik -filter:replies"
    assert build_query("vitalik", True, since=BASE, until=BASE + timedelta(seconds=5)) == (
        f"from:vitalik since_time:{int(BASE.timestamp())} until_time:{int(BASE.timestamp()) + 5}")


def test_history_cutoff_and_pending():
    assert history_cutoff(-1) is None
    assert history_cutoff(10, now=BASE) == BASE - timedelta(days=10)
    cutoff = BASE - timedelta(days=10)
    assert is_pending({"cursor_time": None, "exhausted": False}, cutoff)
    assert is_pending({"cursor_time": BASE, "exhausted": False}, cutoff)
    assert not is_pending({"cursor_time": cutoff - timedelta(seconds=1), "exhausted": False}, cutoff)
    assert not is_pending({"cursor_time": BASE, "exhausted": True}, None)


def test_own_tweets_filters_foreign_and_broken():
    page = {"tweets": [make_tweet(1, BASE), make_tweet(2, BASE, author="Other"),
                       make_tweet(3, BASE, author="VITALIK"), {"id": "4"}]}
    assert [t["id"] for t in own_tweets(page, "vitalik")] == ["1", "3"]
    assert own_tweets({}, "vitalik") == []


def test_history_walks_back_to_the_beginning(storage):
    tweets = [make_tweet(i, BASE - timedelta(hours=i)) for i in range(1, 11)]
    api = FakeApi(tweets, page_size=3)
    for _ in range(20):
        if not is_pending(storage.history_state("vitalik"), None):
            break
        load_history_page(api, storage, "vitalik", False, None)
    assert ids(storage) == list(range(1, 11))
    state = storage.history_state("vitalik")
    assert state["exhausted"] and state["cursor_time"] == BASE - timedelta(hours=10)
    # until_time в фейке включает границу: страница даёт 2 новых твита, затем пустые запросы
    assert sum(1 for q, _ in api.queries if "until_time" in q) == 4 + EMPTY_PAGES_TO_EXHAUST
    assert all(cursor == "" for _, cursor in api.queries)


def test_history_respects_cutoff(storage):
    tweets = [make_tweet(i, BASE - timedelta(days=i)) for i in range(1, 11)]
    api = FakeApi(tweets, page_size=4)
    cutoff = BASE - timedelta(days=5, hours=12)
    while is_pending(storage.history_state("vitalik"), cutoff):
        load_history_page(api, storage, "vitalik", False, cutoff)
    assert ids(storage) == [1, 2, 3, 4, 5]
    assert all(f"since_time:{int(cutoff.timestamp())}" in q for q, _ in api.queries)


def test_history_resumes_after_restart(tmp_path):
    tweets = [make_tweet(i, BASE - timedelta(hours=i)) for i in range(1, 8)]
    s = Storage(tmp_path / "t.db")
    load_history_page(FakeApi(tweets), s, "vitalik", False, None)
    s.close()
    s = Storage(tmp_path / "t.db")
    api = FakeApi(tweets)
    load_history_page(api, s, "vitalik", False, None)
    assert f"until_time:{int((BASE - timedelta(hours=3)).timestamp())}" in api.queries[0][0]
    assert ids(s) == [1, 2, 3, 4, 5]
    s.close()


def test_history_empty_page_does_not_exhaust_immediately(storage):
    load_history_page(FakeApi([]), storage, "vitalik", False, None)
    state = storage.history_state("vitalik")
    assert state["empty_pages"] == 1 and not state["exhausted"]


def test_poll_new_on_empty_account_takes_one_page(storage):
    tweets = [make_tweet(i, BASE - timedelta(hours=i)) for i in range(1, 10)]
    api = FakeApi(tweets, page_size=3)
    assert poll_new(api, storage, "vitalik", False, max_pages=10) == 3
    assert len(api.queries) == 1 and "since_time" not in api.queries[0][0]


def test_poll_new_pages_until_known_tweets(storage):
    old = [make_tweet(i, BASE - timedelta(hours=i)) for i in range(1, 4)]
    for t in old:
        storage.save(tweet_row(t, "vitalik"))
    new = [make_tweet(100 + i, BASE + timedelta(minutes=i)) for i in range(1, 8)]
    api = FakeApi(old + new, page_size=3)
    assert poll_new(api, storage, "vitalik", False, max_pages=10) == 7
    assert ids(storage) == [1, 2, 3] + list(range(101, 108))
    newest_old = int((BASE - timedelta(hours=1)).timestamp())
    assert all(f"since_time:{newest_old}" in q for q, _ in api.queries)
    # второй опрос: только уже сохранённый самый свежий твит
    api.queries.clear()
    assert poll_new(api, storage, "vitalik", False, max_pages=10) == 0
    assert len(api.queries) == 1


def test_poll_new_stops_at_max_pages(storage):
    storage.save(tweet_row(make_tweet(1, BASE), "vitalik"))
    new = [make_tweet(100 + i, BASE + timedelta(minutes=i)) for i in range(1, 20)]
    api = FakeApi(new, page_size=3)
    assert poll_new(api, storage, "vitalik", False, max_pages=2) == 6
    assert len(api.queries) == 2


# api

def make_api(handler, sleeps=None):
    api = TwitterApi("secret", sleep=(sleeps.append if sleeps is not None else lambda s: None))
    api.client = httpx.Client(base_url="https://api.twitterapi.io", headers={"x-api-key": "secret"},
                              transport=httpx.MockTransport(handler))
    return api


def test_api_search_params_and_header():
    seen = {}

    def handler(request):
        seen["key"] = request.headers["x-api-key"]
        seen["params"] = dict(request.url.params)
        seen["path"] = request.url.path
        return httpx.Response(200, json={"tweets": [], "has_next_page": False})

    make_api(handler).search("from:vitalik", cursor="abc")
    assert seen == {"key": "secret", "path": "/twitter/tweet/advanced_search",
                    "params": {"query": "from:vitalik", "queryType": "Latest", "cursor": "abc"}}


def test_api_retries_on_429():
    responses = iter([httpx.Response(429, headers={"Retry-After": "7"}),
                      httpx.Response(200, json={"tweets": [{"id": "1"}]})])
    sleeps = []
    page = make_api(lambda r: next(responses), sleeps).search("q")
    assert page["tweets"] == [{"id": "1"}]
    assert sleeps == [7.0]


@pytest.mark.parametrize("status, match", [(401, "TWITTERAPI_IO_KEY"), (402, "кредиты"), (400, "bad query")])
def test_api_fatal_errors(status, match):
    api = make_api(lambda r: httpx.Response(status, json={"detail": "bad query"}))
    with pytest.raises(ApiError, match=match):
        api.search("q")


def test_api_error_with_status_200():
    api = make_api(lambda r: httpx.Response(200, json={"status": "error", "msg": "boom"}))
    with pytest.raises(ApiError, match="boom"):
        api.search("q")
