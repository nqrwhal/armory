"""calguns.net — vBulletin 6 marketplace RSS feeds (public)."""

from __future__ import annotations

import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime

import feedparser
from bs4 import BeautifulSoup

from ..models import HealthResult, Listing
from .base import AdapterError, SourceAdapter

BASE = "https://www.calguns.net"

# Marketplace sub-forum nodeids (verified 2026-09).
FORUMS: dict[str, int] = {
    "handguns": 121,
    "long_guns": 44,
    "parts_accessories": 45,
    "ammo": 74,
    "reloading": 116,
    "non_firearms": 49,
    "wtb": 48,
    "commercial": 47,
}

_THREAD_ID = re.compile(r"(\d+)(?:-[a-z0-9-]*)?/?$")


def _thread_id(link: str) -> str:
    m = _THREAD_ID.search(link.rstrip("/").rsplit("/", 1)[-1])
    if m:
        return m.group(1)
    return link  # stable fallback: URL itself


def _strip_html(raw_html: str) -> str:
    text = BeautifulSoup(raw_html, "html.parser").get_text(" ", strip=True)
    return re.sub(r"\s+", " ", text).strip()


def _clean_title(title: str) -> str:
    # vBulletin's feed double-escapes entities in some titles ("&amp;amp;")
    while "&amp;" in title or "&lt;" in title or "&gt;" in title:
        unescaped = html.unescape(title)
        if unescaped == title:
            break
        title = unescaped
    return title.strip()


class CalgunsAdapter(SourceAdapter):
    name = "calguns"

    def _forums(self) -> list[str]:
        wanted = self.cfg.forums or list(FORUMS)
        unknown = [f for f in wanted if f not in FORUMS]
        if unknown:
            raise AdapterError(f"calguns: unknown forums {unknown}; valid: {list(FORUMS)}")
        return wanted

    def _feed(self, forum: str):
        resp = self.fetcher.get(f"{BASE}/external", params={"type": "rss2", "nodeid": FORUMS[forum]})
        if resp.status_code != 200:
            raise AdapterError(f"calguns: feed for '{forum}' returned HTTP {resp.status_code}")
        parsed = feedparser.parse(resp.text)
        if parsed.bozo and not parsed.entries:
            raise AdapterError(f"calguns: feed for '{forum}' failed to parse")
        return parsed

    def poll(self, pages: int = 1) -> list[Listing]:
        listings: list[Listing] = []
        for forum in self._forums():
            feed = self._feed(forum)
            for entry in feed.entries:
                link = entry.get("link", "")
                posted: datetime | None = None
                try:
                    posted = parsedate_to_datetime(entry.get("published", ""))
                except (ValueError, TypeError):
                    posted = None
                body_html = entry.get("content", [{}])[0].get("value") if entry.get("content") else entry.get("summary", "")
                listings.append(
                    Listing(
                        source=self.name,
                        external_id=_thread_id(link),
                        url=link,
                        title=_clean_title(entry.get("title", "")),
                        body=_strip_html(body_html or "")[:4000],
                        category=_clean_title(entry.get("category") or forum),
                        author=entry.get("author"),
                        posted_at=posted,
                    )
                )
        return listings

    def search(self, query: str, limit: int = 25) -> list[Listing]:
        resp = self.fetcher.get(f"{BASE}/search", params={"q": query})
        if resp.status_code != 200:
            raise AdapterError(f"calguns: search returned HTTP {resp.status_code}")
        if "log in" in resp.text.lower() and "do that" in resp.text.lower():
            raise AdapterError("calguns: guest search now requires login")
        soup = BeautifulSoup(resp.text, "html.parser")
        out: list[Listing] = []
        seen: set[str] = set()
        for a in soup.find_all("a", href=True):
            href = a["href"]
            if "/forum/" not in href:
                continue
            tail = href.rstrip("/").rsplit("/", 1)[-1]
            m = _THREAD_ID.match(tail)
            title = a.get_text(" ", strip=True)
            if not m or not title or m.group(1) in seen:
                continue
            seen.add(m.group(1))
            out.append(
                Listing(
                    source=self.name,
                    external_id=m.group(1),
                    url=href if href.startswith("http") else BASE + href,
                    title=title,
                )
            )
            if len(out) >= limit:
                break
        return out

    def health(self) -> HealthResult:
        forum = self._forums()[0]
        try:
            resp = self.fetcher.get(f"{BASE}/external", params={"type": "rss2", "nodeid": FORUMS[forum]})
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"network error: {exc}")
        if resp.status_code == 200 and "<rss" in resp.text[:2000].lower():
            return HealthResult(True, f"RSS OK (forum '{forum}')")
        return HealthResult(False, f"unexpected feed response: HTTP {resp.status_code}")
