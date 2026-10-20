import json

from parser import describe_rejection, extract_stats

ETH = {
    "symbol": "ETH",
    "market_id": 0,
    "mark_price": "1899.99",
    "index_price": "1900.86",
    "last_trade_price": "1899.98",
    "best_bid_price": "1899.94",
    "best_ask_price": "1899.95",
}


def test_extract_single_market():
    msg = {"type": "update/market_stats", "market_stats": ETH}
    assert [s["symbol"] for s in extract_stats(msg)] == ["ETH"]


def test_extract_all_markets():
    msg = {
        "type": "update/market_stats",
        "market_stats": {"0": ETH, "1": {"symbol": "BTC", "market_id": 1}},
    }
    assert [s["symbol"] for s in extract_stats(msg)] == ["ETH", "BTC"]


def test_extract_ignores_other_messages():
    assert extract_stats({"type": "pong"}) == []
    assert extract_stats({"market_stats": None}) == []


def _restricted_body():
    return json.dumps(
        {
            "code": 20558,
            "message": "You are accessing Lighter from a restricted jurisdiction.",
        }
    ).encode()


def test_restricted_region_suggests_readonly():
    text = describe_rejection(400, _restricted_body(), readonly=False)
    assert text is not None and "--no-readonly" in text


def test_restricted_region_in_readonly_mode():
    text = describe_rejection(400, _restricted_body(), readonly=True)
    assert text is not None and "--no-readonly" not in text


def test_transient_errors_are_retried():
    assert describe_rejection(429, b"", readonly=False) is None
    assert describe_rejection(503, b"", readonly=False) is None


def test_other_client_errors_are_fatal():
    assert describe_rejection(403, b"forbidden", readonly=False) is not None
