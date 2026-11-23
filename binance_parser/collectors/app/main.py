"""Binance-only online collection; CSV by default, optional existing ClickHouse storage."""
from __future__ import annotations

import asyncio
import logging
import signal
import time

from app.config import load_settings
from app.db import ClickHouseDB
from app.csv_db import CSVDB
from app.exchanges.binance import BinanceFuturesExchange, BinanceSpotExchange

LOG = logging.getLogger('binance.live')


async def collect(db, exchange, method, table, symbols, timeframes=()):
    errors = 0
    for symbol in symbols:
        for timeframe in timeframes or (None,):
            try:
                call = getattr(exchange, method)
                rows = await call(symbol, timeframe) if timeframe else await call(symbol)
                await getattr(db, 'insert_' + table)(rows)
                LOG.info('%s %s %s: %s rows', exchange.market_type, symbol, table, len(rows))
            except Exception as exc:
                errors += 1
                LOG.warning('%s %s %s: %s', exchange.market_type, symbol, table, exc)
                await db.log_event('warning', 'binance', table, str(exc),
                                   dict(symbol=symbol, timeframe=timeframe, market_type=exchange.market_type))
    return errors


async def run_interval(interval, job, stop):
    while not stop.is_set():
        started = time.monotonic()
        try:
            await job()
        except Exception:
            LOG.exception('Collector cycle failed')
        try:
            await asyncio.wait_for(stop.wait(), timeout=max(interval-(time.monotonic()-started), .1))
        except asyncio.TimeoutError:
            pass


async def run(settings=None, once=False):
    settings = settings or load_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level), format='%(asctime)s %(levelname)s %(message)s')
    db = ClickHouseDB(settings) if settings.storage == 'clickhouse' else CSVDB(settings)
    await db.connect()
    exchanges = [BinanceSpotExchange(settings), BinanceFuturesExchange(settings)]
    tasks, errors = [], 0
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    try:
        for exchange in exchanges:
            try:
                instruments = await exchange.get_instruments()
                await db.insert_instruments(instruments)
                symbols = exchange.map_symbols(list(settings.symbols), instruments)
            except Exception as exc:
                errors += 1
                LOG.warning('Instrument discovery failed: %s', exc)
                symbols = list(settings.symbols)
            jobs = [
                ('fetch_candles', 'candles', settings.candles_interval_seconds, settings.timeframes),
                ('fetch_ticker', 'tickers', settings.tickers_interval_seconds, ()),
            ]
            if exchange.market_type == 'usdt_futures':
                jobs = [
                    ('fetch_funding_rate', 'funding_rates', settings.funding_interval_seconds, ()),
                    ('fetch_open_interest', 'open_interest', settings.open_interest_interval_seconds, ()),
                ]
            for method, table, interval, frames in jobs:
                async def job(e=exchange, m=method, t=table, s=symbols, f=frames):
                    return await collect(db, e, m, t, s, f)
                if once:
                    errors += await job()
                else:
                    tasks.append(asyncio.create_task(run_interval(interval, job, stop)))
        if not once:
            LOG.info('Polling started. Historical outages require history.py backfill; polling is not a complete trade tape.')
            await stop.wait()
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for exchange in exchanges:
            await exchange.close()
        await db.close()
    return 2 if errors else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))
