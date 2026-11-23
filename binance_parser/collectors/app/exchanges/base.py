from __future__ import annotations

import asyncio
import json
import logging
import random
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import Settings

logger = logging.getLogger(__name__)


class ExchangeAPIError(RuntimeError):
    pass


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def ms_to_datetime(value: Any) -> datetime:
    if value in (None, ""):
        return now_utc()
    return datetime.fromtimestamp(int(float(value)) / 1000, tz=timezone.utc)


def to_float(value: Any) -> float | None:
    if value in (None, "", "null"):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    if value in (None, "", "null"):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def raw_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str, separators=(",", ":"))


def split_common_quote(symbol: str) -> tuple[str, str]:
    upper = symbol.upper().replace("-", "")
    for quote in ("USDT", "USDC", "USD", "BTC", "ETH"):
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)], quote
    return upper, ""


def timeframe_to_seconds(timeframe: str) -> int:
    value = timeframe.strip()
    unit = value[-1].lower()
    amount = int(value[:-1]) if value[:-1].isdigit() else 1

    if unit == "m":
        return amount * 60
    if unit == "h":
        return amount * 60 * 60
    if unit == "d":
        return amount * 24 * 60 * 60
    if unit == "w":
        return amount * 7 * 24 * 60 * 60

    if value.isdigit():
        return int(value) * 60
    return 60


def close_time_from_open(open_time: datetime, timeframe: str) -> datetime:
    return open_time + timedelta(seconds=timeframe_to_seconds(timeframe))


class BaseExchange(ABC):
    name: str
    market_type: str
    base_url: str

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.http = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(settings.http_timeout_seconds),
            headers={"User-Agent": "binance-data-parser/2.0"},
        )

    async def close(self) -> None:
        await self.http.aclose()

    async def _get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        max_attempts = self.settings.http_max_retries + 1
        last_exc: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            response: httpx.Response | None = None
            try:
                response = await self.http.get(path, params=params)
                if response.status_code >= 400:
                    message = f"HTTP {response.status_code} for {path}"
                    if response.status_code not in (418, 429, 500, 502, 503, 504):
                        raise RuntimeError(f'{message}: public endpoint rejected request')
                    if attempt >= max_attempts:
                        raise ExchangeAPIError(f"{message}: {response.text[:500]}")
                    delay = self._retry_delay(attempt, response)
                    logger.warning("%s %s. Retrying in %.2fs", self.name, message, delay)
                    await asyncio.sleep(delay)
                    continue

                return response.json()
            except (httpx.RequestError, ValueError, ExchangeAPIError) as exc:
                last_exc = exc
                if attempt >= max_attempts:
                    break
                delay = self._retry_delay(attempt, response)
                logger.warning("%s request failed: %s. Retrying in %.2fs", self.name, exc, delay)
                await asyncio.sleep(delay)

        raise ExchangeAPIError(f"{self.name} request failed after {max_attempts} attempts: {last_exc}")

    def _retry_delay(self, attempt: int, response: httpx.Response | None = None) -> float:
        if response is not None and response.status_code in (418, 429):
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    return max(float(retry_after), 1.0)
                except ValueError:
                    pass

        base = self.settings.http_backoff_base_seconds
        jitter = random.uniform(0, 0.25 * max(base, 0.1))
        return min(base * (2 ** (attempt - 1)) + jitter, 30.0)

    def map_symbols(self, symbols: list[str], instruments: list[dict[str, Any]] | None = None) -> list[str]:
        return [symbol.upper() for symbol in symbols]

    @abstractmethod
    async def get_instruments(self) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_candles(self, symbol: str, timeframe: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_ticker(self, symbol: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_funding_rate(self, symbol: str) -> list[dict[str, Any]]:
        raise NotImplementedError

    @abstractmethod
    async def fetch_open_interest(self, symbol: str) -> list[dict[str, Any]]:
        raise NotImplementedError
