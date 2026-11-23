from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Settings:
    clickhouse_host: str = 'localhost'
    clickhouse_port: int = 8123
    clickhouse_database: str = 'crypto'
    clickhouse_user: str = 'default'
    clickhouse_password: str = ''
    storage: str = 'csv'
    output_dir: str = 'data/live'
    symbols: tuple = ('BTCUSDT',)
    timeframes: tuple = ('1m',)
    candles_interval_seconds: int = 60
    tickers_interval_seconds: int = 10
    funding_interval_seconds: int = 300
    open_interest_interval_seconds: int = 60
    http_timeout_seconds: float = 20
    http_max_retries: int = 4
    http_backoff_base_seconds: float = 1
    log_level: str = 'INFO'


def load_settings():
    root = Path(__file__).resolve().parents[2]
    # Simple KEY=VALUE files; process environment always takes precedence.
    env_file = root / '.env'
    if env_file.exists():
        for line in env_file.read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            key, separator, value = line.partition('=')
            if not separator or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key.strip()):
                raise ValueError('Invalid .env line; use KEY=VALUE')
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                value = value[1:-1]
            os.environ.setdefault(key.strip(), value)
    def sequence(name, fallback):
        return tuple(dict.fromkeys(s.strip().upper() for s in os.getenv(name, fallback).split(',') if s.strip()))
    kwargs = dict(
        clickhouse_host=os.getenv('CLICKHOUSE_HOST', 'localhost'),
        clickhouse_port=int(os.getenv('CLICKHOUSE_PORT', '8123')),
        clickhouse_database=os.getenv('CLICKHOUSE_DATABASE', 'crypto'),
        clickhouse_user=os.getenv('CLICKHOUSE_USER', 'default'),
        clickhouse_password=os.getenv('CLICKHOUSE_PASSWORD', ''),
        storage=os.getenv('STORAGE', 'csv').lower(), output_dir=os.getenv('OUTPUT_DIR', 'data/live'),
        symbols=sequence('SYMBOLS', 'BTCUSDT'), timeframes=tuple(s.lower() for s in sequence('TIMEFRAMES', '1m')),
        log_level=os.getenv('LOG_LEVEL', 'INFO').upper(),
    )
    for field, env, default in [
        ('candles_interval_seconds', 'COLLECT_CANDLES_INTERVAL_SECONDS', 60),
        ('tickers_interval_seconds', 'COLLECT_TICKERS_INTERVAL_SECONDS', 10),
        ('funding_interval_seconds', 'COLLECT_FUNDING_INTERVAL_SECONDS', 300),
        ('open_interest_interval_seconds', 'COLLECT_OPEN_INTEREST_INTERVAL_SECONDS', 60),
        ('http_max_retries', 'HTTP_MAX_RETRIES', 4),
    ]:
        kwargs[field] = int(os.getenv(env, str(default)))
    kwargs['http_timeout_seconds'] = float(os.getenv('HTTP_TIMEOUT_SECONDS', '20'))
    kwargs['http_backoff_base_seconds'] = float(os.getenv('HTTP_BACKOFF_BASE_SECONDS', '1'))
    result = Settings(**kwargs)
    if result.storage not in ('csv', 'clickhouse'):
        raise ValueError('STORAGE must be csv or clickhouse')
    if not result.symbols or any(not re.fullmatch(r'[A-Z0-9]{5,30}', s) for s in result.symbols):
        raise ValueError('Invalid SYMBOLS')
    if not result.timeframes or any(t not in ('1m','3m','5m','15m','30m','1h','2h','4h','6h','8h','12h','1d','3d','1w') for t in result.timeframes):
        raise ValueError('Unsupported TIMEFRAMES')
    if min(result.candles_interval_seconds, result.tickers_interval_seconds, result.funding_interval_seconds,
           result.open_interest_interval_seconds, result.http_timeout_seconds) <= 0 or result.http_max_retries < 0:
        raise ValueError('Intervals/timeouts must be positive and retries nonnegative')
    return result
