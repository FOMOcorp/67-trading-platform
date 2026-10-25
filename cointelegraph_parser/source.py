"""Публичные sitemap и HTML Cointelegraph; без внутренних API."""

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree

from bs4 import BeautifulSoup

BASE_URL = "https://cointelegraph.com"
USER_AGENT = "CointelegraphResearch/0.1"
SITEMAP_NS = {"s": "http://www.sitemaps.org/schemas/sitemap/0.9"}


def canonical_url(url):
    """Убираем tracking-параметры; ссылки другого источника не принимаем."""
    parts = urlsplit(url)
    if (parts.scheme != "https" or parts.netloc != urlsplit(BASE_URL).netloc
            or parts.username or parts.password):
        raise ValueError(f"Unexpected source URL: {url}")
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def article_id(url):
    # Это наш стабильный ID по URL, а не выдуманный внутренний ID издателя.
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def sitemap_locations(xml):
    root = ElementTree.fromstring(xml)
    kind = root.tag.rsplit("}", 1)[-1]
    if kind not in {"sitemapindex", "urlset"}:
        raise ValueError("Expected a sitemap XML document")
    return kind, [node.text.strip() for node in root.findall("s:*/s:loc", SITEMAP_NS)
                  if node.text and node.text.strip()]


class NoRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Перенос источника требует проверки, а не автоматического обхода.
        return None


class Source:
    def __init__(self):
        self.delay = 2.0
        self.last_request = 0.0
        self.opener = build_opener(NoRedirects())
        robots_url = BASE_URL + "/robots.txt"
        robots_text = self._read(robots_url).decode("utf-8")
        self.robots = RobotFileParser(robots_url)
        self.robots.parse(robots_text.splitlines())
        crawl_delay = self.robots.crawl_delay(USER_AGENT)
        rate = self.robots.request_rate(USER_AGENT)
        self.delay = max(self.delay, crawl_delay or 0,
                         rate.seconds / rate.requests if rate else 0)

    def _read(self, url):
        time.sleep(max(0, self.delay - (time.monotonic() - self.last_request)))
        self.last_request = time.monotonic()
        request = Request(url, headers={"User-Agent": USER_AGENT})
        with self.opener.open(request, timeout=30) as response:
            directives = response.headers.get("X-Robots-Tag", "").lower()
            if any(word in directives for word in ("noarchive", "nosnippet", "none")):
                raise PermissionError(f"X-Robots-Tag restricts storage: {url}")
            return response.read()

    def get(self, url):
        url = canonical_url(url)
        if not self.robots.can_fetch(USER_AGENT, url):
            raise PermissionError(f"robots.txt disallows {url}")
        return self._read(url)

    def urls(self, history, reverse=False, start_month=None):
        kind, maps = sitemap_locations(self.get(BASE_URL + "/sitemap.xml"))
        if kind != "sitemapindex":
            raise ValueError("Expected the published monthly sitemap index")
        # Только реально опубликованные месячные карты; даты не перебираем.
        maps = sorted({canonical_url(url) for url in maps
                       if re.fullmatch(r"/sitemap/articles/\d{4}-\d{2}\.xml",
                                       urlsplit(url).path)})
        if not maps:
            raise ValueError("No monthly article sitemaps found")
        if not history:
            maps = maps[-2:]  # Сохраняет переход через границу месяца.
        elif reverse:
            if start_month:
                maps = [url for url in maps if url.rsplit("/", 1)[-1][:7] <= start_month]
            maps.reverse()
            if not maps:
                raise ValueError(f"No article sitemaps at or before {start_month}")
        seen = set()
        for sitemap in maps:
            kind, urls = sitemap_locations(self.get(sitemap))
            if kind != "urlset":
                raise ValueError(f"Expected article URLs in {sitemap}")
            for url in urls:
                url = canonical_url(url)
                section = urlsplit(url).path.strip("/").split("/", 1)[0]
                if section in {"press-releases", "research", "commissioned"}:
                    continue
                if url not in seen:
                    seen.add(url)
                    yield url


def news_metadata(soup):
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            value = json.loads(script.string or script.get_text())
        except (ValueError, TypeError):
            continue
        nodes = value if isinstance(value, list) else [value]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            graph = node.get("@graph", [])
            if not isinstance(graph, list):
                graph = []
            for candidate in [node] + graph:
                if not isinstance(candidate, dict):
                    continue
                types = candidate.get("@type") or []
                if types == "NewsArticle" or isinstance(types, list) and "NewsArticle" in types:
                    return candidate
    raise ValueError("NewsArticle JSON-LD missing; page not saved")


def parse_article(html, url):
    url = canonical_url(url)
    soup = BeautifulSoup(html, "html.parser")
    for meta in soup.select('meta[name="robots"], meta[name="CointelegraphResearch"]'):
        if any(word in meta.get("content", "").lower()
               for word in ("noarchive", "nosnippet", "none")):
            raise PermissionError(f"Page directives restrict storage: {url}")
    metadata = news_metadata(soup)
    actual_language = metadata.get("inLanguage", "en")
    if not isinstance(actual_language, str) or actual_language.split("-")[0] != "en":
        raise ValueError(f"Unexpected article language: {actual_language}")
    canonical = soup.select_one('link[rel="canonical"]')
    if canonical:
        page_url = canonical_url(canonical.get("href", ""))
        if page_url != url:
            raise ValueError(f"Canonical URL differs: {page_url}")
    title = metadata.get("headline")
    date = metadata.get("datePublished")
    if not isinstance(title, str) or not title.strip() or not isinstance(date, str):
        raise ValueError("Article title or publication date missing")
    title = title.strip()
    published = datetime.fromisoformat(date.replace("Z", "+00:00"))
    if published.tzinfo is None:
        raise ValueError("Publication date has no timezone")
    # Селектор проверен на публичном EN HTML 2026-10-16.
    body = soup.select_one('[data-testid="post__body"]')
    if body is None or not title:
        raise ValueError("Title or full article body missing; page not saved")
    for unwanted in body.select("script, style, nav, aside, form"):
        unwanted.decompose()
    text = " ".join(body.stripped_strings)
    if not text:
        raise ValueError("Empty article body; page not saved")
    tags = list(dict.fromkeys(tag.get_text(" ", strip=True)
                             for tag in soup.select('[data-testid="block-with-tags"] '
                                                    '[data-testid="post-tag"]')))
    tag_slugs = [urlsplit(tag.get("href", "")).path.rsplit("/", 1)[-1]
                 for tag in soup.select('[data-testid="block-with-tags"] a[href]')]
    clues = " ".join([title, text] + tags + tag_slugs)
    patterns = {"BTC": r"\b(?:BTC|bitcoin)\b",
                "ETH": r"\b(?:ETH|ethereum|ether)\b"}
    symbols = [symbol for symbol, pattern in patterns.items() if re.search(pattern, clues, re.I)]
    return {
        "id": article_id(url), "language": "en", "url": url,
        "title": title, "text": text,
        "published_at": published.astimezone(timezone.utc).isoformat(),
        "tags": tags, "symbols": symbols,
        "first_seen_at": datetime.now(timezone.utc).isoformat(),
    }
