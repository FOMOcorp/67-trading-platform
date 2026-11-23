from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from datetime import datetime, timezone
from typing import Any


from app.config import Settings

logger = logging.getLogger(__name__)


INSTRUMENT_COLUMNS = [
    "exchange",
    "market_type",
    "symbol",
    "base_asset",
    "quote_asset",
    "instrument_type",
    "contract_type",
    "status",
    "tick_size",
    "step_size",
    "raw",
    "updated_at",
]

CANDLE_COLUMNS = [
    "exchange",
    "market_type",
    "symbol",
    "timeframe",
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trades",
    "available_at",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "taker_sell_base_volume",
    "taker_sell_quote_volume",
    "trade_imbalance",
    "raw",
]

TICKER_COLUMNS = [
    "exchange",
    "market_type",
    "symbol",
    "event_time",
    "price",
    "mark_price",
    "index_price",
    "bid_price",
    "ask_price",
    "volume_24h",
    "quote_volume_24h",
    "received_at",
    "raw",
]

FUNDING_COLUMNS = [
    "exchange",
    "market_type",
    "symbol",
    "funding_time",
    "funding_rate",
    "next_funding_time",
    "mark_price",
    "index_price",
    "raw",
]

OPEN_INTEREST_COLUMNS = [
    "exchange",
    "market_type",
    "symbol",
    "event_time",
    "open_interest",
    "open_interest_value",
    "single_open_interest",
    "raw",
]

LOG_COLUMNS = [
    "timestamp",
    "level",
    "exchange",
    "collector",
    "message",
    "context",
]

FUNDING_SNAPSHOT_COLUMNS = ['exchange', 'market_type', 'symbol', 'event_time',
    'funding_rate_snapshot', 'next_funding_time', 'mark_price', 'index_price', 'received_at', 'raw']
BASIS_COLUMNS = ['exchange', 'market_type', 'symbol', 'event_time', 'available_at',
    'spot_close', 'futures_close', 'mark_price', 'index_price', 'premium_index',
    'futures_spot_basis_bps', 'mark_index_gap_bps', 'raw']


class ClickHouseDB:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.client: Any | None = None
        self._lock = asyncio.Lock()

    async def connect(self) -> None:
        import clickhouse_connect
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', self.settings.clickhouse_database):
            raise ValueError('Invalid ClickHouse database name')
        for attempt in range(1, 31):
            try:
                self.client = await asyncio.to_thread(
                    clickhouse_connect.get_client,
                    host=self.settings.clickhouse_host,
                    port=self.settings.clickhouse_port,
                    username=self.settings.clickhouse_user,
                    password=self.settings.clickhouse_password,
                    database=self.settings.clickhouse_database,
                    connect_timeout=10,
                    send_receive_timeout=30,
                )
                await asyncio.to_thread(self.client.command, "SELECT 1")
                schema = Path(__file__).resolve().parents[2] / 'clickhouse/init/001_create_tables.sql'
                sql = schema.read_text(encoding='utf-8').replace('crypto.', self.settings.clickhouse_database + '.')
                sql = sql.replace('DATABASE IF NOT EXISTS crypto', 'DATABASE IF NOT EXISTS ' + self.settings.clickhouse_database)
                for statement in sql.split(';'):
                    if statement.strip():
                        await asyncio.to_thread(self.client.command, statement)
                logger.info("Connected to ClickHouse %s:%s/%s", self.settings.clickhouse_host, self.settings.clickhouse_port, self.settings.clickhouse_database)
                return
            except Exception as exc:
                delay = min(2 * attempt, 10)
                logger.warning("ClickHouse is not ready yet: %s. Retrying in %ss", exc, delay)
                await asyncio.sleep(delay)

        raise RuntimeError("Could not connect to ClickHouse after 30 attempts")

    async def close(self) -> None:
        if self.client is not None:
            await asyncio.to_thread(self.client.close)

    async def insert_instruments(self, rows: list[dict[str, Any]]) -> None:
        await self._insert("instruments", rows, INSTRUMENT_COLUMNS)

    async def insert_candles(self, rows: list[dict[str, Any]]) -> None:
        await self._insert("candles", rows, CANDLE_COLUMNS)

    async def insert_tickers(self, rows: list[dict[str, Any]]) -> None:
        await self._insert("tickers", rows, TICKER_COLUMNS)

    async def insert_funding_rates(self, rows: list[dict[str, Any]]) -> None:
        await self._insert("funding_settlements", rows, FUNDING_COLUMNS)

    async def insert_open_interest(self, rows: list[dict[str, Any]]) -> None:
        await self._insert("open_interest", rows, OPEN_INTEREST_COLUMNS)

    async def insert_funding_snapshots(self, rows):
        await self._insert('funding_snapshots', rows, FUNDING_SNAPSHOT_COLUMNS)

    async def insert_basis(self, rows):
        await self._insert('basis', rows, BASIS_COLUMNS)

    async def log_event(
        self,
        level: str,
        exchange: str,
        collector: str,
        message: str,
        context: dict[str, Any] | str | None = None,
    ) -> None:
        if isinstance(context, str):
            context_json = context
        else:
            context_json = json.dumps(context or {}, ensure_ascii=False, default=str, separators=(",", ":"))

        row = {
            "timestamp": datetime.now(timezone.utc),
            "level": level.upper(),
            "exchange": exchange,
            "collector": collector,
            "message": message,
            "context": context_json,
        }

        try:
            await self._insert("collector_logs", [row], LOG_COLUMNS)
        except Exception:
            logger.exception("Could not write collector log to ClickHouse")

    async def _insert(self, table: str, rows: list[dict[str, Any]], columns: list[str]) -> None:
        if not rows:
            return
        if self.client is None:
            raise RuntimeError("ClickHouse client is not connected")

        data = [[row.get(column) for column in columns] for row in rows]
        async with self._lock:
            await asyncio.to_thread(self.client.insert, table, data, column_names=columns)
