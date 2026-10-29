"""Клиент twitterapi.io: только поиск твитов (advanced_search).

Документация: https://docs.twitterapi.io. Ключ передаётся в заголовке x-api-key.
"""

import logging
import random
import time

import httpx

from . import metrics

log = logging.getLogger("twitter_parser.api")

BASE_URL = "https://api.twitterapi.io"
SEARCH_PATH = "/twitter/tweet/advanced_search"
# временные ошибки: повторяем с паузой
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 5


class ApiError(Exception):
    """Ошибка, которую повтор не исправит: неверный ключ, нет баланса, неверный запрос."""


class TwitterApi:
    def __init__(self, api_key: str, proxy_url: str | None = None, sleep=time.sleep):
        self.client = httpx.Client(
            base_url=BASE_URL,
            headers={"x-api-key": api_key},
            proxy=proxy_url,
            timeout=30,
        )
        self.sleep = sleep

    def search(self, query: str, cursor: str = "") -> dict:
        """Одна страница поиска, новые твиты первыми (до ~20 твитов).
        Ответ: {tweets: [...], has_next_page, next_cursor}."""
        params = {"query": query, "queryType": "Latest"}
        if cursor:
            params["cursor"] = cursor
        return self._get(SEARCH_PATH, params)

    def _get(self, path: str, params: dict) -> dict:
        for attempt in range(1, MAX_ATTEMPTS + 1):
            try:
                response = self.client.get(path, params=params)
            except httpx.TransportError as e:
                metrics.API_REQUESTS.labels(status="network").inc()
                if attempt == MAX_ATTEMPTS:
                    raise
                log.warning("сеть: %s, повтор %d/%d", e, attempt, MAX_ATTEMPTS)
                self.sleep(backoff(attempt))
                continue

            metrics.API_REQUESTS.labels(status=str(response.status_code)).inc()
            if response.status_code in RETRY_STATUSES and attempt < MAX_ATTEMPTS:
                delay = retry_after(response.headers.get("Retry-After")) or backoff(attempt)
                log.warning("HTTP %d от twitterapi.io, пауза %.0f с", response.status_code, delay)
                self.sleep(delay)
                continue
            if response.status_code == 401:
                raise ApiError("неверный TWITTERAPI_IO_KEY (HTTP 401)")
            if response.status_code == 402:
                raise ApiError("на балансе twitterapi.io закончились кредиты (HTTP 402)")
            if response.status_code >= 400:
                raise ApiError(f"HTTP {response.status_code}: {error_detail(response)}")

            data = response.json()
            # некоторые ошибки приходят с кодом 200
            if isinstance(data, dict) and data.get("status") == "error":
                raise ApiError(f"twitterapi.io: {data.get('msg') or data.get('message')}")
            return data
        raise AssertionError("unreachable")

    def close(self) -> None:
        self.client.close()


def backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** attempt) * random.uniform(1, 1.5)


def retry_after(value: str | None) -> float | None:
    try:
        return max(1.0, float(value))
    except (TypeError, ValueError):
        return None


def error_detail(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text[:200]
    if isinstance(data, dict):
        return str(data.get("detail") or data.get("msg") or data.get("message") or data)[:200]
    return str(data)[:200]
