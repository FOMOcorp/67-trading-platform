"""Сбор твитов через поиск twitterapi.io.

Новые твиты: запрос `from:<account> since_time:<самый свежий сохранённый>`,
страницы листаются, пока попадаются несохранённые твиты.

История: запрос `from:<account> until_time:<самый старый сохранённый>` —
одна страница (~20 твитов) за раз, вглубь. Позиция хранится в таблице
history_state, поэтому после перезапуска загрузка продолжается с того же места.
Курсор API не сохраняется: каждая страница — новый запрос от самого старого твита.
"""

import logging
from datetime import datetime, timedelta, timezone

from . import metrics
from .storage import Storage, parse_created_at, tweet_row

log = logging.getLogger("twitter_parser.collector")

# поиск иногда отдаёт пустую страницу при живой истории: конец — только после нескольких подряд
EMPTY_PAGES_TO_EXHAUST = 3


def build_query(account: str, include_replies: bool, since: datetime | None = None,
                until: datetime | None = None) -> str:
    parts = [f"from:{account}"]
    if not include_replies:
        parts.append("-filter:replies")
    if since is not None:
        parts.append(f"since_time:{int(since.timestamp())}")
    if until is not None:
        parts.append(f"until_time:{int(until.timestamp())}")
    return " ".join(parts)


def history_cutoff(days: int, now: datetime | None = None) -> datetime | None:
    """Граница по дате: None — вся история."""
    if days < 0:
        return None
    return (now or datetime.now(timezone.utc)) - timedelta(days=days)


def is_pending(state: dict, cutoff: datetime | None) -> bool:
    """Есть ли ещё что грузить для аккаунта с таким состоянием."""
    if state["exhausted"]:
        return False
    if cutoff is None or state["cursor_time"] is None:
        return True
    return state["cursor_time"] > cutoff


def own_tweets(page: dict, account: str) -> list[dict]:
    """Твиты страницы, написанные самим аккаунтом (поиск может подмешать чужие)."""
    tweets = page.get("tweets") or []
    return [
        t for t in tweets
        if t.get("id") and t.get("createdAt")
        and ((t.get("author") or {}).get("userName") or account).lower() == account
    ]


def save_tweets(storage: Storage, tweets: list[dict], account: str, source: str) -> int:
    saved = 0
    for tweet in tweets:
        row = tweet_row(tweet, account)
        if storage.save(row):
            metrics.record_saved(row, source)
            saved += 1
            preview = (row["text"] or "").replace("\n", " ")[:80]
            log.info("[@%s] %s %s %s", account, source, row["tweet_id"], preview)
    return saved


def poll_new(api, storage: Storage, account: str, include_replies: bool, max_pages: int) -> int:
    """Догружает твиты новее самого свежего сохранённого. Для пустого аккаунта —
    одна страница последних твитов, остальное загрузит история."""
    newest = storage.newest_created_at(account)
    query = build_query(account, include_replies, since=newest)
    cursor, saved = "", 0
    for _ in range(max_pages if newest else 1):
        page = api.search(query, cursor)
        page_saved = save_tweets(storage, own_tweets(page, account), account, "live")
        saved += page_saved
        cursor = page.get("next_cursor") or ""
        # страница без новых твитов — дальше только уже сохранённые
        if not page_saved or not page.get("has_next_page") or not cursor:
            break
    else:
        if newest:
            log.warning("[@%s] новых твитов больше %d страниц, остаток заберёт история или следующий опрос",
                        account, max_pages)
    return saved


def load_history_page(api, storage: Storage, account: str, include_replies: bool,
                      cutoff: datetime | None) -> int:
    """Одна страница истории старше курсора. Возвращает число новых твитов."""
    state = storage.history_state(account)
    query = build_query(account, include_replies, since=cutoff, until=state["cursor_time"])
    tweets = own_tweets(api.search(query), account)

    if state["cursor_time"] is not None:
        # until_time может включать границу: учитываем только то, что строго старше
        tweets = [t for t in tweets if parse_created_at(t["createdAt"]) < state["cursor_time"]]
    if cutoff is not None:
        tweets = [t for t in tweets if parse_created_at(t["createdAt"]) >= cutoff]

    saved = save_tweets(storage, tweets, account, "history")
    if tweets:
        oldest = min(parse_created_at(t["createdAt"]) for t in tweets)
        state["cursor_time"] = oldest
        state["empty_pages"] = 0
        metrics.HISTORY_OLDEST.labels(account=account).set(oldest.timestamp())
    else:
        state["empty_pages"] += 1
    state["exhausted"] = state["empty_pages"] >= EMPTY_PAGES_TO_EXHAUST
    storage.set_history_state(account, state)
    return saved
