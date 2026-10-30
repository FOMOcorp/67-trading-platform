#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["python-socks>=2.5"]
# ///

"""
Выгрузка исторических свечей (OHLCV) с биржи Lighter в CSV.

Использует публичный REST-эндпоинт /api/v1/candles: ключи и авторизация
не нужны. API отдаёт не больше 500 свечей за запрос, поэтому длинные
диапазоны скачиваются постранично.

Как и parser.py, умеет ходить через прокси: --proxy или переменная PROXY_URL
(socks5://user:pass@host:1080, socks4://host:1080, http://host:3128).

Примеры:
    uv run candles.py -m ETH -s 2025-09-01                       # часовые свечи с 1 сентября до сейчас
    uv run candles.py -m ETH -r 1m -s 2025-10-01 -e 2025-10-08   # минутки за неделю
    uv run candles.py -m 0 -r 1d -s 2025-01-01 -o eth_daily.csv  # по market_id, свой файл
    uv run candles.py --list-markets                             # список рынков и их market_id

Диапазон полуоткрытый: [start, end). Даты без часового пояса считаются UTC.
"""

import argparse
import csv
import difflib
import http.client
import json
import os
import ssl
import sys
import time
from datetime import datetime, timezone
from functools import partial
from pathlib import Path
from typing import Optional
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPSHandler, ProxyHandler, Request, build_opener

__version__ = "0.2.0"

BASE_URLS = {
    "mainnet": "https://mainnet.zklighter.elliot.ai",
    "testnet": "https://testnet.zklighter.elliot.ai",
}

# длительность свечи в миллисекундах
RESOLUTIONS = {
    "1m": 60_000,
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "30m": 30 * 60_000,
    "1h": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
}

MAX_CANDLES_PER_REQUEST = 500

# count_back — МИНИМУМ свечей в ответе: при большом значении API добирает
# свечи раньше start. С 1 он возвращает ровно диапазон (до 500 последних).
COUNT_BACK = 1

# Standard-аккаунты (и анонимные запросы): 60 запросов в минуту на IP.
DEFAULT_DELAY = 1.1
MAX_RETRIES = 6

CSV_FIELDS = [
    "timestamp",
    "datetime_utc",
    "open",
    "high",
    "low",
    "close",
    "volume_base",
    "volume_quote",
    "last_trade_id",
]


PROXY_SCHEMES = ("socks5", "socks4", "http")


class ApiError(Exception):
    """Ошибка, после которой продолжать бессмысленно."""


class ProxyRejected(OSError):
    """Прокси отказал (неверный логин/пароль, запрет и т. п.) — повторять бессмысленно."""


def log(text: str) -> None:
    print(f"# {text}", file=sys.stderr, flush=True)


def now_ms() -> int:
    return int(time.time() * 1000)


def ms_to_str(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime(
        "%Y-%m-%d %H:%M:%S"
    )


# ---------------------------------------------------------------- аргументы


def parse_time(value: str) -> int:
    """Дата/время -> unix-время в миллисекундах.

    Принимает: 2025-10-01, 2025-10-01T12:30, "2025-10-01 12:30:00",
    2025-10-01T12:30:00Z, +03:00 и т. п., unix-время в секундах или
    миллисекундах, а также слово now.
    """
    v = value.strip()
    if v.lower() == "now":
        return now_ms()
    if v.isdigit():
        n = int(v)
        return n if n >= 10**11 else n * 1000  # секунды -> миллисекунды
    if v.endswith(("Z", "z")):
        v = v[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"не понимаю дату {value!r}; формат: 2025-10-01 или 2025-10-01T12:30"
        ) from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------- HTTP


def describe_http_error(status: int, body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    try:
        payload = json.loads(text)
        message = payload.get("message") or text
        code = payload.get("code")
    except (ValueError, AttributeError):
        message, code = text, None

    if code == 20558 or "restricted jurisdiction" in message.lower():
        return (
            f"HTTP {status}: Lighter ограничивает доступ к REST API с этого IP. "
            "Если в проекте есть прокси, укажите его: --proxy или PROXY_URL. "
            "Подробности: https://lighter.xyz/terms"
        )
    return f"HTTP {status}: {message or 'запрос отклонён сервером'}"


def describe_proxy(proxy: Optional[str]) -> str:
    """Адрес прокси без логина и пароля, для логов."""
    if not proxy:
        return ""
    parts = urlsplit(proxy)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}"


def check_proxy_url(proxy: str) -> None:
    parts = urlsplit(proxy)
    if parts.scheme not in PROXY_SCHEMES:
        raise ApiError(
            f"неподдерживаемый прокси {describe_proxy(proxy)!r}: "
            f"нужен {', '.join(s + '://' for s in PROXY_SCHEMES)}"
        )
    try:
        port = parts.port
    except ValueError:
        port = None
    if not parts.hostname or not port:
        raise ApiError("в адресе прокси нужны хост и порт, например socks5://host:1080")


class _ProxiedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS-соединение, которое открывает TCP-туннель через python-socks
    (та же библиотека, через которую ходит в WebSocket parser.py)."""

    def __init__(self, *args, proxy_url: str, **kwargs):
        super().__init__(*args, **kwargs)
        self._proxy_url = proxy_url

    def connect(self) -> None:
        from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError
        from python_socks.sync import Proxy

        try:
            sock = Proxy.from_url(self._proxy_url).connect(
                dest_host=self.host, dest_port=self.port, timeout=self.timeout
            )
        except (ProxyConnectionError, ProxyTimeoutError) as e:
            raise ConnectionError(f"прокси {describe_proxy(self._proxy_url)} недоступен: {e}") from e
        except ProxyError as e:
            raise ProxyRejected(f"прокси {describe_proxy(self._proxy_url)} отказал: {e}") from e
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


class _ProxiedHTTPSHandler(HTTPSHandler):
    def __init__(self, proxy_url: str):
        super().__init__(context=ssl.create_default_context())
        self._proxy_url = proxy_url

    def https_open(self, req):
        conn = partial(_ProxiedHTTPSConnection, proxy_url=self._proxy_url)
        return self.do_open(conn, req, context=self._context)


def make_opener(proxy: Optional[str]):
    if not proxy:
        return build_opener()  # учитывает системные HTTPS_PROXY/NO_PROXY
    check_proxy_url(proxy)
    try:
        import python_socks  # noqa: F401
    except ImportError:
        raise ApiError(
            "для --proxy нужна библиотека python-socks: запустите через "
            "`uv run candles.py` или `pip install python-socks`"
        ) from None
    # ProxyHandler({}) отключает системные прокси: ходим только через указанный
    return build_opener(ProxyHandler({}), _ProxiedHTTPSHandler(proxy))


class Client:
    """Минимальный клиент REST API Lighter с паузой между запросами и повторами."""

    def __init__(
        self,
        base_url: str,
        delay: float = DEFAULT_DELAY,
        timeout: float = 30,
        proxy: Optional[str] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.delay = delay
        self.timeout = timeout
        self._opener = make_opener(proxy)
        self._next_request_at = 0.0
        self.requests_made = 0

    def get(self, path: str, params: dict) -> dict:
        url = f"{self.base_url}{path}?{urlencode(params)}"
        request = Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": f"lighter-price-parser/{__version__}",
            },
        )
        for attempt in range(MAX_RETRIES + 1):
            pause = self._next_request_at - time.monotonic()
            if pause > 0:
                time.sleep(pause)
            self._next_request_at = time.monotonic() + self.delay
            self.requests_made += 1

            try:
                with self._opener.open(request, timeout=self.timeout) as resp:
                    # parse_float=str: цены и объёмы сохраняем ровно как их отдал API
                    return json.loads(resp.read(), parse_float=str)
            except HTTPError as e:
                body = e.read()
                # 429/405 — превышен лимит запросов, 5xx — сбой на стороне биржи
                if e.code in (405, 429) or e.code >= 500:
                    if attempt == MAX_RETRIES:
                        raise ApiError(describe_http_error(e.code, body)) from e
                    retry_after = e.headers.get("Retry-After", "")
                    wait = (
                        float(retry_after)
                        if retry_after.replace(".", "", 1).isdigit()
                        else min(60, 5 * 2**attempt)
                    )
                    log(f"HTTP {e.code}, повтор через {wait:.0f} с")
                    time.sleep(wait)
                    continue
                raise ApiError(describe_http_error(e.code, body)) from e
            except (URLError, TimeoutError, ConnectionError) as e:
                reason = getattr(e, "reason", e)  # URLError заворачивает исходную ошибку
                if isinstance(reason, ProxyRejected):
                    raise ApiError(str(reason)) from e
                if attempt == MAX_RETRIES:
                    raise ApiError(f"сеть недоступна: {reason}") from e
                wait = min(60, 2**attempt)
                log(f"ошибка сети ({reason}), повтор через {wait} с")
                time.sleep(wait)

        raise ApiError("не удалось выполнить запрос")  # недостижимо


# ---------------------------------------------------------------- рынки


def get_markets(client: Client) -> list:
    data = client.get("/api/v1/orderBooks", {})
    return data.get("order_books") or []


def resolve_market(markets: list, market: str) -> dict:
    """Находит рынок по символу (ETH, LINK/USDC) или по market_id."""
    m = market.strip()
    if m.isdigit():
        for item in markets:
            if item.get("market_id") == int(m):
                return item
        raise ApiError(f"рынок с market_id={m} не найден; см. --list-markets")

    by_symbol = {str(item.get("symbol", "")).upper(): item for item in markets}
    found = by_symbol.get(m.upper())
    if found:
        return found

    hints = difflib.get_close_matches(m.upper(), by_symbol.keys(), n=5)
    hint = f" Возможно, вы имели в виду: {', '.join(hints)}." if hints else ""
    raise ApiError(f"монета {market!r} не найдена.{hint} Список: --list-markets")


def print_markets(markets: list) -> None:
    print(f"{'market_id':>9}  {'symbol':<16} {'type':<5} status")
    for item in sorted(markets, key=lambda x: x.get("market_id", 0)):
        print(
            f"{item.get('market_id'):>9}  {item.get('symbol', ''):<16} "
            f"{item.get('market_type', ''):<5} {item.get('status', '')}"
        )


# ---------------------------------------------------------------- свечи


def fetch_candles(get_page, market_id: int, resolution: str, start_ms: int, end_ms: int, on_page=None) -> list:
    """Скачивает все свечи с start_ms <= t < end_ms, по возрастанию t.

    get_page(market_id, resolution, start_ms, end_ms) должна возвращать ответ
    /api/v1/candles. API отдаёт свечи с start <= t < end, но не больше 500
    последних, поэтому идём от конца к началу: следующая страница
    заканчивается там, где началась предыдущая. Пропуски (минуты без
    сделок, время до листинга) не стоят лишних запросов. Свечи вне
    диапазона, если API их всё же пришлёт, отбрасываются.
    """
    pages = []
    cursor = end_ms
    while cursor > start_ms:
        data = get_page(market_id, resolution, start_ms, cursor)
        raw = data.get("c") or []
        page = sorted(
            (c for c in raw if start_ms <= int(c["t"]) < cursor),
            key=lambda c: int(c["t"]),
        )
        if not page:
            break
        pages.append(page)
        if on_page:
            on_page(page, sum(len(p) for p in pages))
        if len(page) < MAX_CANDLES_PER_REQUEST:
            break  # в диапазоне больше ничего нет
        cursor = int(page[0]["t"])

    return [c for page in reversed(pages) for c in page]


def drop_unfinished(candles: list, resolution: str, now: int) -> list:
    """Убирает последнюю свечу, если её период ещё не закончился."""
    step = RESOLUTIONS[resolution]
    return [c for c in candles if int(c["t"]) + step <= now]


def candle_to_row(c: dict) -> dict:
    t = int(c["t"])
    return {
        "timestamp": t,
        "datetime_utc": ms_to_str(t),
        "open": c.get("o", 0),
        "high": c.get("h", 0),
        "low": c.get("l", 0),
        "close": c.get("c", 0),
        # API не присылает нулевые значения, поэтому подставляем 0
        "volume_base": c.get("v", 0),
        "volume_quote": c.get("V", 0),
        "last_trade_id": c.get("i", ""),
    }


def write_csv(path: Path, candles: list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for c in candles:
            writer.writerow(candle_to_row(c))
    tmp.replace(path)  # файл появляется только целиком


def default_output(symbol: str, resolution: str, start_ms: int, end_ms: int) -> Path:
    safe = symbol.replace("/", "-")
    start = ms_to_str(start_ms)[:10]
    end = ms_to_str(end_ms)[:10]
    return Path(f"lighter_{safe}_{resolution}_{start}_{end}.csv")


# ---------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Выгрузка исторических свечей (OHLCV) Lighter в CSV",
        epilog="Диапазон полуоткрытый: [start, end). Время без часового пояса — UTC.",
    )
    p.add_argument("-m", "--market", help="символ (ETH, BTC, LINK/USDC) или market_id")
    p.add_argument(
        "-r",
        "--resolution",
        choices=list(RESOLUTIONS),
        default="1h",
        help="таймфрейм свечей (по умолчанию 1h)",
    )
    p.add_argument(
        "-s",
        "--start",
        type=parse_time,
        help="с какой даты: 2025-10-01, 2025-10-01T12:00, unix-время",
    )
    p.add_argument(
        "-e",
        "--end",
        type=parse_time,
        default=None,
        help="до какой даты, не включая (по умолчанию — сейчас)",
    )
    p.add_argument(
        "-o",
        "--output",
        type=Path,
        help="CSV-файл (по умолчанию lighter_<монета>_<таймфрейм>_<с>_<по>.csv)",
    )
    p.add_argument(
        "--include-partial",
        action="store_true",
        help="оставить последнюю, ещё не закрытую свечу",
    )
    p.add_argument(
        "--list-markets",
        action="store_true",
        help="показать рынки и их market_id и выйти",
    )
    p.add_argument("--testnet", action="store_true", help="использовать testnet")
    p.add_argument(
        "--proxy",
        default=os.environ.get("PROXY_URL") or None,
        help="прокси: socks5://user:pass@host:1080, socks4://host:1080 или "
        "http://host:3128 (по умолчанию из PROXY_URL)",
    )
    p.add_argument(
        "--delay",
        type=float,
        default=DEFAULT_DELAY,
        help=f"пауза между запросами, с (по умолчанию {DEFAULT_DELAY}: лимит 60 запросов/мин)",
    )
    return p


def run(args) -> int:
    client = Client(
        BASE_URLS["testnet" if args.testnet else "mainnet"],
        delay=args.delay,
        proxy=args.proxy,
    )
    if args.proxy:
        log(f"через прокси {describe_proxy(args.proxy)}")
    markets = get_markets(client)

    if args.list_markets:
        print_markets(markets)
        return 0

    market = resolve_market(markets, args.market)
    market_id = int(market["market_id"])
    symbol = str(market.get("symbol", market_id))

    current = now_ms()
    end_ms = args.end if args.end is not None else current
    start_ms = args.start
    if start_ms >= end_ms:
        raise ApiError("дата начала должна быть раньше даты окончания")

    step = RESOLUTIONS[args.resolution]
    expected = -(-(end_ms - start_ms) // step)
    log(
        f"{symbol} (market_id={market_id}), {args.resolution}, "
        f"{ms_to_str(start_ms)} → {ms_to_str(end_ms)} UTC; "
        f"до {expected} свечей, ~{-(-expected // MAX_CANDLES_PER_REQUEST)} запросов"
    )

    def get_page(mid, res, s, e):
        return client.get(
            "/api/v1/candles",
            {
                "market_id": mid,
                "resolution": res,
                "start_timestamp": s,
                "end_timestamp": e,
                "count_back": COUNT_BACK,
            },
        )

    def on_page(page, total):
        done = (end_ms - int(page[0]["t"])) / (end_ms - start_ms) * 100
        log(f"{total} свечей, дошли до {ms_to_str(int(page[0]['t']))} ({min(done, 100):.0f}%)")

    candles = fetch_candles(get_page, market_id, args.resolution, start_ms, end_ms, on_page)
    if not args.include_partial:
        candles = drop_unfinished(candles, args.resolution, current)

    if not candles:
        log("за этот период свечей нет (возможно, рынок тогда ещё не торговался)")
        return 1

    output = args.output or default_output(symbol, args.resolution, start_ms, end_ms)
    write_csv(output, candles)

    first, last = int(candles[0]["t"]), int(candles[-1]["t"])
    missing = (last - first) // step + 1 - len(candles)
    gaps = f", пропусков (периодов без сделок): {missing}" if missing else ""
    log(
        f"готово: {len(candles)} свечей {ms_to_str(first)} … {ms_to_str(last)} UTC "
        f"→ {output}{gaps}"
    )
    return 0


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    if not args.list_markets:
        if not args.market:
            parser.error("укажите монету: -m ETH (или --list-markets)")
        if args.start is None:
            parser.error("укажите дату начала: -s 2025-10-01")
    try:
        sys.exit(run(args))
    except ApiError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        print("\nПрервано", file=sys.stderr)
        sys.exit(130)


if __name__ == "__main__":
    main()
