import argparse
import csv

import pytest

from candles import (
    MAX_CANDLES_PER_REQUEST,
    RESOLUTIONS,
    ApiError,
    Client,
    check_proxy_url,
    describe_http_error,
    describe_proxy,
    drop_unfinished,
    fetch_candles,
    parse_time,
    resolve_market,
    write_csv,
)

MIN = RESOLUTIONS["1m"]
T0 = 1_760_000_040_000  # 2025-10-09 08:54:00 UTC, начало минуты


def make_candles(start, count, step=MIN, skip=()):
    return [
        {"t": start + i * step, "o": "1", "h": "2", "l": "0.5", "c": "1.5", "v": "10", "V": "15", "i": i}
        for i in range(count)
        if i not in skip
    ]


class FakeApi:
    """Ведёт себя как реальный /api/v1/candles (проверено на mainnet):
    свечи с start <= t < end, но не меньше count_back штук (добирает свечи
    раньше start) и не больше 500 последних."""

    def __init__(self, candles, count_back=1):
        self.candles = candles
        self.count_back = count_back
        self.calls = 0

    def __call__(self, market_id, resolution, start, end):
        self.calls += 1
        before_end = [c for c in self.candles if c["t"] < end]
        in_range = [c for c in before_end if c["t"] >= start]
        n = min(max(len(in_range), self.count_back), MAX_CANDLES_PER_REQUEST)
        return {"code": 200, "r": resolution, "c": before_end[-n:] if n else []}


def ts(candles):
    return [c["t"] for c in candles]


def test_paginates_over_long_range():
    data = make_candles(T0, 1234)
    api = FakeApi(data)
    got = fetch_candles(api, 0, "1m", T0, T0 + 1234 * MIN)
    assert ts(got) == ts(data)
    assert api.calls == 3  # 500 + 500 + 234


def test_exact_multiple_of_page_size():
    data = make_candles(T0, 1000)
    api = FakeApi(data)
    got = fetch_candles(api, 0, "1m", T0, T0 + 1000 * MIN)
    assert ts(got) == ts(data)
    assert api.calls == 2  # дошли ровно до start — лишний пустой запрос не нужен


def test_respects_range_bounds():
    data = make_candles(T0, 100)
    got = fetch_candles(FakeApi(data), 0, "1m", T0 + 10 * MIN, T0 + 20 * MIN)
    assert ts(got) == [T0 + i * MIN for i in range(10, 20)]


def test_gaps_and_data_before_listing():
    data = make_candles(T0, 700, skip=range(100, 300))  # 200 минут без сделок
    api = FakeApi(data)
    # просим с запасом в прошлое, где данных нет
    got = fetch_candles(api, 0, "1m", T0 - 10_000 * MIN, T0 + 700 * MIN)
    assert ts(got) == ts(data)
    assert len(got) == 500
    assert api.calls == 2


@pytest.mark.parametrize("count_back", [1, 10, 500])
def test_extra_candles_before_start_are_dropped(count_back):
    data = make_candles(T0, 2000)
    start, end = T0 + 1200 * MIN, T0 + 1203 * MIN
    got = fetch_candles(FakeApi(data, count_back), 0, "1m", start, end)
    assert ts(got) == [start, start + MIN, start + 2 * MIN]


def test_empty_range():
    assert fetch_candles(FakeApi([]), 0, "1h", T0, T0 + 10 * MIN) == []


def test_no_duplicates_and_sorted():
    data = make_candles(T0, 1600)
    got = fetch_candles(FakeApi(data), 0, "1m", T0, T0 + 1600 * MIN)
    t = ts(got)
    assert t == sorted(set(t))


def test_drop_unfinished():
    data = make_candles(T0, 3)
    now = T0 + 2 * MIN + 30_000  # третья свеча ещё идёт
    assert ts(drop_unfinished(data, "1m", now)) == [T0, T0 + MIN]


@pytest.mark.parametrize(
    "value, expected",
    [
        ("2025-10-09", 1759968000000),
        ("2025-10-09T08:54", T0),
        ("2025-10-09 08:54:00", T0),
        ("2025-10-09T08:54:00Z", T0),
        ("2025-10-09T11:54:00+03:00", T0),
        ("1760000040", T0),
        ("1760000040000", T0),
    ],
)
def test_parse_time(value, expected):
    assert parse_time(value) == expected


def test_parse_time_invalid():
    with pytest.raises(argparse.ArgumentTypeError):
        parse_time("вчера")


MARKETS = [
    {"market_id": 0, "symbol": "ETH", "market_type": "perp", "status": "active"},
    {"market_id": 1, "symbol": "BTC", "market_type": "perp", "status": "active"},
    {"market_id": 2050, "symbol": "LINK/USDC", "market_type": "spot", "status": "active"},
]


def test_resolve_by_symbol_case_insensitive():
    assert resolve_market(MARKETS, "eth")["market_id"] == 0
    assert resolve_market(MARKETS, "link/usdc")["market_id"] == 2050


def test_resolve_by_id():
    assert resolve_market(MARKETS, "1")["symbol"] == "BTC"


def test_resolve_unknown_suggests():
    with pytest.raises(ApiError, match="ETH"):
        resolve_market(MARKETS, "ETHH")
    with pytest.raises(ApiError):
        resolve_market(MARKETS, "999")


def test_restricted_region_message():
    body = b'{"code": 20558, "message": "You are accessing Lighter from a restricted jurisdiction."}'
    assert "--proxy" in describe_http_error(400, body)


def test_write_csv(tmp_path):
    data = [{"t": T0, "o": "4328.11", "h": "4356.46", "l": "4325.52", "c": "4335.82", "V": "99.5", "i": 7}]
    out = tmp_path / "sub" / "eth.csv"
    write_csv(out, data)
    rows = list(csv.DictReader(out.open(encoding="utf-8")))
    assert rows == [
        {
            "timestamp": str(T0),
            "datetime_utc": "2025-10-09 08:54:00",
            "open": "4328.11",
            "high": "4356.46",
            "low": "4325.52",
            "close": "4335.82",
            "volume_base": "0",  # API опускает нулевые значения
            "volume_quote": "99.5",
            "last_trade_id": "7",
        }
    ]
    assert not (tmp_path / "sub" / "eth.csv.part").exists()


def test_describe_proxy_hides_credentials():
    assert describe_proxy("socks5://user:secret@10.0.0.1:1080") == "socks5://10.0.0.1:1080"
    assert describe_proxy(None) == ""


@pytest.mark.parametrize("url", ["socks5://u:p@host:1080", "socks4://host:1080", "http://host:3128"])
def test_check_proxy_url_ok(url):
    check_proxy_url(url)


@pytest.mark.parametrize("url", ["https://host:3128", "socks5h://host:1080", "socks5://host", "host:1080"])
def test_check_proxy_url_rejects(url):
    with pytest.raises(ApiError):
        check_proxy_url(url)


def test_client_rejects_bad_proxy_before_any_request():
    with pytest.raises(ApiError, match="прокси"):
        Client("https://example.invalid", proxy="ftp://host:21")
