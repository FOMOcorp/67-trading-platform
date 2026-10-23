#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["websockets>=14.0", "prometheus-client>=0.20"]
# ///

"""
Парсер цен с биржи Lighter через публичный WebSocket.

Подписывается на канал market_stats/all и выводит/сохраняет цены
(mark, index, last, best bid/ask) при каждом изменении.

Ключи и авторизация не нужны: рыночные данные публичные.

По умолчанию используется read-only подключение Lighter: скрипт только
читает данные, а этот режим доступен и в регионах с ограничениями.

Примеры:
    uv run parser.py                        # все рынки, вывод в консоль
    uv run parser.py -s ETH BTC SOL         # только выбранные символы
    uv run parser.py -s ETH --csv eth.csv   # + запись в CSV
    uv run parser.py --once                 # один снимок цен и выход
    uv run parser.py --testnet              # тестовая сеть
"""

import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone
from typing import Optional

import websockets
from prometheus_client import Counter, Gauge, start_http_server
from websockets.exceptions import InvalidStatus, WebSocketException

# метрики Prometheus, отдаются при --metrics-port
WS_CONNECTED = Gauge("lighter_ws_connected", "1, если WebSocket подключён")
RECONNECTS = Counter(
    "lighter_reconnects_total", "Переподключения по причинам", ["reason"]
)
FRAMES = Counter("lighter_frames_received_total", "Сообщения, полученные от сервера")
ROWS = Counter("lighter_rows_written_total", "Изменения цен (строки вывода и CSV)")
LAST_UPDATE = Gauge(
    "lighter_last_update_timestamp_seconds", "Время последнего изменения цены (unix)"
)
MARKETS = Gauge("lighter_markets_seen", "Рынки, по которым пришли цены")
CSV_SIZE = Gauge("lighter_csv_size_bytes", "Размер CSV-файла")

WS_URLS = {
    "mainnet": "wss://mainnet.zklighter.elliot.ai/stream",
    "testnet": "wss://testnet.zklighter.elliot.ai/stream",
}

PING_EVERY = 60  # сервер закрывает соединение, если от клиента нет фреймов 2 минуты
MAX_BACKOFF = 30  # максимальная пауза между переподключениями, секунды

CSV_FIELDS = [
    "ts_utc",
    "symbol",
    "market_id",
    "mark_price",
    "index_price",
    "last_trade_price",
    "best_bid_price",
    "best_ask_price",
    "mid_price",
    "current_funding_rate",
    "daily_price_change",
    "daily_quote_token_volume",
]

# по этим полям определяем, что цена изменилась
PRICE_FIELDS = (
    "mark_price",
    "index_price",
    "last_trade_price",
    "best_bid_price",
    "best_ask_price",
)


class FatalError(Exception):
    """Ошибка, после которой переподключаться бессмысленно."""


def extract_stats(msg: dict) -> list:
    """Достаёт список market_stats-объектов из сообщения.

    Для market_stats/{id} приходит один объект, для market_stats/all —
    словарь {market_id: объект}. Обрабатываем оба варианта.
    """
    ms = msg.get("market_stats")
    if not isinstance(ms, dict):
        return []
    if "symbol" in ms:
        return [ms]
    return [v for v in ms.values() if isinstance(v, dict) and "symbol" in v]


def describe_rejection(status: int, body: bytes, readonly: bool) -> Optional[str]:
    """Если ошибку рукопожатия повторять бессмысленно, возвращает текст для
    пользователя, иначе None (тогда стоит переподключиться)."""
    if status == 429 or status >= 500:
        return None  # лимиты и сбои сервера: подождём и попробуем снова

    text = body.decode("utf-8", errors="replace").strip() if body else ""
    try:
        payload = json.loads(text)
        server_msg = payload.get("message", text)
        code = payload.get("code")
    except (ValueError, AttributeError):
        server_msg, code = text, None

    if code == 20558 or "restricted jurisdiction" in server_msg.lower():
        hint = (
            "Lighter ограничивает доступ из вашего региона. Для чтения "
            "рыночных данных у биржи есть read-only режим: запустите без "
            "--no-readonly."
            if not readonly
            else "Доступ ограничен даже в read-only режиме. Подробности в условиях "
            "использования: https://lighter.xyz/terms"
        )
        return f"HTTP {status}: {server_msg}\n{hint}"

    return f"HTTP {status}: {server_msg or 'запрос отклонён сервером'}"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


async def ping_loop(ws) -> None:
    while True:
        await asyncio.sleep(PING_EVERY)
        await ws.send(json.dumps({"type": "ping"}))


def print_row(ts: str, s: dict) -> None:
    print(
        f"{ts}  {s['symbol']:<12} "
        f"mark={s.get('mark_price')}  index={s.get('index_price')}  "
        f"last={s.get('last_trade_price')}  "
        f"bid={s.get('best_bid_price')}  ask={s.get('best_ask_price')}",
        flush=True,
    )


def log(text: str) -> None:
    print(f"# {text}", file=sys.stderr, flush=True)


async def stream(args) -> int:
    """Возвращает код выхода процесса."""
    url = WS_URLS["testnet" if args.testnet else "mainnet"]
    if args.readonly:
        url += "?readonly=true"

    wanted = {x.upper() for x in args.symbols} if args.symbols else None
    last_seen = {}
    matched = set()

    csv_file = None
    writer = None
    if args.csv:
        csv_file = open(args.csv, "a", newline="", encoding="utf-8")
        writer = csv.DictWriter(csv_file, fieldnames=CSV_FIELDS, extrasaction="ignore")
        if csv_file.tell() == 0:
            writer.writeheader()
        CSV_SIZE.set_function(lambda: os.path.getsize(args.csv))

    backoff = 1
    try:
        while True:
            try:
                async with websockets.connect(
                    url, ping_interval=None, max_size=None
                ) as ws:
                    await ws.send(
                        json.dumps({"type": "subscribe", "channel": "market_stats/all"})
                    )
                    log(f"connected: {url}")
                    WS_CONNECTED.set(1)
                    backoff = 1
                    pinger = asyncio.create_task(ping_loop(ws))
                    try:
                        async for raw in ws:
                            try:
                                msg = json.loads(raw)
                            except ValueError:
                                continue
                            mtype = msg.get("type", "")
                            FRAMES.inc()

                            if mtype == "ping":
                                await ws.send(json.dumps({"type": "pong"}))
                                continue
                            if mtype == "pong":
                                continue

                            stats = extract_stats(msg)
                            if not stats:
                                if args.debug:
                                    log(f"other message: {raw[:300]}")
                                continue

                            ts = now_iso()
                            for s in stats:
                                symbol = str(s["symbol"]).upper()
                                if wanted and symbol not in wanted:
                                    continue
                                matched.add(symbol)
                                key = tuple(s.get(f) for f in PRICE_FIELDS)
                                if last_seen.get(symbol) == key:
                                    continue  # цены не менялись
                                last_seen[symbol] = key
                                ROWS.inc()
                                LAST_UPDATE.set_to_current_time()
                                MARKETS.set(len(last_seen))

                                if not args.quiet:
                                    print_row(ts, s)
                                if writer:
                                    writer.writerow({"ts_utc": ts, **s})
                                    csv_file.flush()

                            if args.once:
                                if wanted and not matched:
                                    log(
                                        "ни один из символов не найден: "
                                        + ", ".join(sorted(wanted))
                                    )
                                    return 1
                                return 0
                    finally:
                        pinger.cancel()
                        WS_CONNECTED.set(0)
            except InvalidStatus as e:
                resp = e.response
                reason = describe_rejection(
                    resp.status_code, resp.body or b"", args.readonly
                )
                if reason:
                    raise FatalError(reason) from e
                RECONNECTS.labels(reason=f"http_{resp.status_code}").inc()
                log(f"HTTP {resp.status_code}; reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
            except (WebSocketException, OSError, asyncio.TimeoutError) as e:
                RECONNECTS.labels(reason="connection_lost").inc()
                log(f"connection lost: {e!r}; reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, MAX_BACKOFF)
    finally:
        if csv_file:
            csv_file.close()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Парсер цен Lighter (WebSocket market_stats)"
    )
    p.add_argument(
        "-s", "--symbols", nargs="+", help="фильтр по символам, например ETH BTC SOL"
    )
    p.add_argument("--csv", help="дописывать изменения цен в CSV-файл")
    p.add_argument("--once", action="store_true", help="получить один снимок и выйти")
    p.add_argument("--testnet", action="store_true", help="использовать testnet")
    p.add_argument(
        "--readonly",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="read-only подключение Lighter (по умолчанию включено; "
        "--no-readonly отключает)",
    )
    p.add_argument("--quiet", action="store_true", help="не печатать строки в консоль")
    p.add_argument(
        "--metrics-port",
        type=int,
        help="отдавать метрики Prometheus на http://0.0.0.0:PORT/metrics",
    )
    p.add_argument(
        "--debug", action="store_true", help="показывать неопознанные сообщения"
    )
    return p


def main() -> None:
    args = build_parser().parse_args()
    if args.metrics_port:
        start_http_server(args.metrics_port)
        log(f"metrics: http://0.0.0.0:{args.metrics_port}/metrics")
    try:
        sys.exit(asyncio.run(stream(args)))
    except FatalError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        sys.exit(2)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
