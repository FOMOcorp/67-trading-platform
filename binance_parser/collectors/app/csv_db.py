"""Append-only online CSV storage. Files are partitioned by UTC day and market.

Deduplication keys are recovered from today's files after restart. CSV is a
single-writer storage; use separate output roots for separate processes.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from app.db import (INSTRUMENT_COLUMNS, CANDLE_COLUMNS, TICKER_COLUMNS, FUNDING_COLUMNS,
                    OPEN_INTEREST_COLUMNS, FUNDING_SNAPSHOT_COLUMNS, BASIS_COLUMNS, LOG_COLUMNS)

TABLES = dict(instruments=INSTRUMENT_COLUMNS, candles=CANDLE_COLUMNS, tickers=TICKER_COLUMNS,
              funding_rates=FUNDING_COLUMNS, open_interest=OPEN_INTEREST_COLUMNS,
              funding_snapshots=FUNDING_SNAPSHOT_COLUMNS, basis=BASIS_COLUMNS, collector_logs=LOG_COLUMNS)
KEYS = dict(instruments=['symbol','updated_at'], candles=['symbol','timeframe','open_time'],
            funding_rates=['symbol','funding_time'], tickers=['symbol','event_time'],
            open_interest=['symbol','event_time'], funding_snapshots=['symbol','event_time'],
            basis=['symbol','event_time'], collector_logs=['timestamp','collector','symbol'])


class CSVDB:
    def __init__(self, settings):
        self.root = Path(settings.output_dir).resolve()
        self.seen = {}

    async def connect(self):
        self.root.mkdir(parents=True, exist_ok=True)

    async def close(self):
        pass

    async def _insert(self, table, rows, columns=None):
        columns = columns or TABLES[table]
        for raw in rows:
            row = {k:(v.isoformat() if isinstance(v, datetime) else '' if v is None else str(v)) for k,v in raw.items()}
            stamp_key = {'instruments':'updated_at','candles':'open_time','funding_rates':'funding_time',
                         'collector_logs':'timestamp'}.get(table, 'event_time')
            stamp = raw[stamp_key]
            event = stamp if isinstance(stamp, datetime) else datetime.fromisoformat(stamp)
            day = event.astimezone(timezone.utc).date().isoformat()
            market = raw.get('market_type', 'system')
            path = self.root / table / market / f'{day}.csv'
            path.parent.mkdir(parents=True, exist_ok=True)
            if path not in self.seen:
                if len(self.seen) >= 64:
                    self.seen.pop(next(iter(self.seen)))
                self.seen[path] = set()
                if path.exists():
                    with path.open(encoding='utf-8', newline='') as stream:
                        reader = csv.DictReader(stream)
                        if reader.fieldnames != columns:
                            raise ValueError(f'Existing CSV has a different schema; choose another --output: {path}')
                        self.seen[path].update(tuple(r.get(k,'') for k in KEYS[table]) for r in reader)
            key = tuple(row.get(k,'') for k in KEYS[table])
            if key in self.seen[path]:
                continue
            fresh = not path.exists() or not path.stat().st_size
            with path.open('a', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=columns, extrasaction='ignore')
                if fresh:
                    writer.writeheader()
                writer.writerow(row)
                stream.flush()
            self.seen[path].add(key)

    def __getattr__(self, name):
        if name.startswith('insert_') and name[7:] in TABLES:
            async def insert(rows):
                await self._insert(name[7:], rows)
            return insert
        raise AttributeError(name)

    async def log_event(self, level, exchange, collector, message, context=None):
        await self._insert('collector_logs', [dict(timestamp=datetime.now(timezone.utc), level=level.upper(),
            exchange=exchange, collector=collector, message=message, context=json.dumps(context or {}))])
