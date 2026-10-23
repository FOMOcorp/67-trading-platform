"""Настройки из переменных окружения (.env)."""

import os
from dataclasses import dataclass
from pathlib import Path


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_id: int
    api_hash: str
    phone: str | None
    folder: str
    session_path: Path
    db_path: Path
    proxy_url: str | None
    folder_refresh_seconds: int
    catchup_limit: int
    history_days: int
    history_delay_seconds: int
    metrics_port: int
    log_level: str


def _required(env: dict, name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(f"переменная {name} не задана (см. .env.example)")
    return value


def _int(env: dict, name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(f"{name} должна быть целым числом, получено {raw!r}") from None


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    _required(env, "TG_API_ID")
    return Config(
        api_id=_int(env, "TG_API_ID", 0),
        api_hash=_required(env, "TG_API_HASH"),
        phone=env.get("TG_PHONE", "").strip() or None,
        folder=env.get("TG_FOLDER", "").strip() or "crypto",
        session_path=Path(env.get("TG_SESSION_PATH", "").strip() or "data/telegram.session"),
        db_path=Path(env.get("DB_PATH", "").strip() or "data/messages.db"),
        proxy_url=env.get("PROXY_URL", "").strip() or None,
        folder_refresh_seconds=_int(env, "FOLDER_REFRESH_SECONDS", 300),
        catchup_limit=_int(env, "CATCHUP_LIMIT", 500),
        history_days=_int(env, "HISTORY_DAYS", 90),
        history_delay_seconds=_int(env, "HISTORY_DELAY_SECONDS", 5),
        metrics_port=_int(env, "METRICS_PORT", 9101),
        log_level=env.get("LOG_LEVEL", "").strip().upper() or "INFO",
    )
