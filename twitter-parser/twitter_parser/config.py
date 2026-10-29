"""Настройки из переменных окружения (.env)."""

import os
import re
from dataclasses import dataclass
from pathlib import Path

# правила X для имени пользователя: латиница, цифры, _, до 15 символов
USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    api_key: str
    accounts: tuple[str, ...]
    db_path: Path
    proxy_url: str | None
    include_replies: bool
    poll_seconds: int
    poll_max_pages: int
    history_days: int
    history_delay_seconds: float
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


def _float(env: dict, name: str, default: float) -> float:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        raise ConfigError(f"{name} должна быть числом, получено {raw!r}") from None


def _bool(env: dict, name: str, default: bool) -> bool:
    raw = env.get(name, "").strip().lower()
    if not raw:
        return default
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    raise ConfigError(f"{name} должна быть true или false, получено {raw!r}")


def parse_accounts(raw: str) -> tuple[str, ...]:
    """Список аккаунтов через запятую или пробел, @ в начале можно не писать.
    Имена приводятся к нижнему регистру: в X они не зависят от регистра."""
    accounts: list[str] = []
    for item in re.split(r"[,\s]+", raw):
        name = item.strip().removeprefix("@").lower()
        if not name:
            continue
        if not USERNAME_RE.match(name):
            raise ConfigError(f"некорректное имя аккаунта в TW_ACCOUNTS: {item!r}")
        if name not in accounts:
            accounts.append(name)
    if not accounts:
        raise ConfigError("TW_ACCOUNTS пуст (см. .env.example)")
    return tuple(accounts)


def load_config(env: dict | None = None) -> Config:
    env = os.environ if env is None else env
    cfg = Config(
        api_key=_required(env, "TWITTERAPI_IO_KEY"),
        accounts=parse_accounts(_required(env, "TW_ACCOUNTS")),
        db_path=Path(env.get("DB_PATH", "").strip() or "data/tweets.db"),
        proxy_url=env.get("PROXY_URL", "").strip() or None,
        include_replies=_bool(env, "INCLUDE_REPLIES", False),
        poll_seconds=_int(env, "POLL_SECONDS", 300),
        poll_max_pages=_int(env, "POLL_MAX_PAGES", 10),
        history_days=_int(env, "HISTORY_DAYS", 365),
        history_delay_seconds=_float(env, "HISTORY_DELAY_SECONDS", 1),
        metrics_port=_int(env, "METRICS_PORT", 9103),
        log_level=env.get("LOG_LEVEL", "").strip().upper() or "INFO",
    )
    if cfg.poll_seconds < 60:
        raise ConfigError("POLL_SECONDS должна быть не меньше 60")
    if cfg.poll_max_pages < 1:
        raise ConfigError("POLL_MAX_PAGES должна быть не меньше 1")
    return cfg
