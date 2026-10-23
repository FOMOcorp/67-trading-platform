"""Разбор PROXY_URL в параметры TelegramClient.

Поддерживаются:
    socks5://user:pass@host:1080   (socks5h:// — то же самое)
    socks4://host:1080
    http://user:pass@host:3128
    mtproto://<secret>@host:443    (MTProxy)
"""

from urllib.parse import unquote, urlsplit

from telethon import connection

SOCKS_HTTP = {"socks5": "socks5", "socks5h": "socks5", "socks4": "socks4", "http": "http"}


def parse_proxy(url: str | None) -> dict:
    """Возвращает kwargs для TelegramClient: {} или {"proxy": ..., ["connection": ...]}."""
    if not url:
        return {}

    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if not parts.hostname or not parts.port:
        raise ValueError(f"в PROXY_URL нужны хост и порт: {url!r}")

    if scheme in SOCKS_HTTP:
        proxy = {
            "proxy_type": SOCKS_HTTP[scheme],
            "addr": parts.hostname,
            "port": parts.port,
            "rdns": True,
        }
        if parts.username:
            proxy["username"] = unquote(parts.username)
        if parts.password:
            proxy["password"] = unquote(parts.password)
        return {"proxy": proxy}

    if scheme == "mtproto":
        if not parts.username:
            raise ValueError("для mtproto:// укажите секрет: mtproto://<secret>@host:port")
        return {
            "connection": connection.ConnectionTcpMTProxyRandomizedIntermediate,
            "proxy": (parts.hostname, parts.port, unquote(parts.username)),
        }

    raise ValueError(f"неизвестная схема прокси {scheme!r}: нужна socks5, socks4, http или mtproto")


def describe_proxy(url: str | None) -> str:
    """Адрес прокси без логина и пароля, для логов."""
    if not url:
        return "без прокси"
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}:{parts.port}"
