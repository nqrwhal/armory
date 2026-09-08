"""calguns.net — vBulletin 6 marketplace: RSS feeds + full thread-list scraping (public)."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

import feedparser
from bs4 import BeautifulSoup

from ..models import HealthResult, Listing, ThreadRow
from .base import AdapterError, SourceAdapter

BASE = "https://www.calguns.net"

# vBulletin renders dates in the board's local timezone.
BOARD_TZ = ZoneInfo("America/Los_Angeles")

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

# Known-good slugs so backfill can start without a feed round-trip; the RSS
# <category domain> is the source of truth and overrides these when seen.
FORUM_SLUGS: dict[str, str] = {
    "handguns": "/forum/marketplace/private-firearms-sales-handguns",
    "long_guns": "/forum/marketplace/private-firearms-sales-long-guns",
}

_THREAD_ID = re.compile(r"(\d+)(?:-[a-z0-9-]*)?/?$")
_VB_DATE = re.compile(r"(\d{2})-(\d{2})-(\d{4}),\s*(\d{1,2}):(\d{2})\s*(AM|PM)", re.IGNORECASE)
_COUNTS = re.compile(r"([\d,]+)\s*responses?\s*([\d,]+)\s*views", re.IGNORECASE)
_SOLD = re.compile(r"\b(sold|spf|withdrawn)\b", re.IGNORECASE)
_PRICE = re.compile(r"\$\s?(\d{1,5}(?:,\d{3})*(?:\.\d{2})?)", re.IGNORECASE)


def parse_vb_date(raw: str | None) -> datetime | None:
    """'09-02-2026, 8:09 PM' (board-local) → aware UTC datetime."""
    if not raw:
        return None
    m = _VB_DATE.search(raw)
    if not m:
        return None
    mo, d, y, hh, mm, ap = m.groups()
    hour = int(hh) % 12 + (12 if ap.upper() == "PM" else 0)
    try:
        local = datetime(int(y), int(mo), int(d), hour, int(mm), tzinfo=BOARD_TZ)
    except ValueError:
        return None
    return local.astimezone(timezone.utc)


def parse_title(title: str) -> dict:
    """Extract calguns title conventions: intent, asking price, sold markers.

    Location is deliberately left to the geo resolver — city names overlap
    heavily with gun vocabulary ("Springfield"), so title-level extraction
    would be less reliable than scanning the full text for real place names.
    """
    out: dict = {"wants_to": None, "price_usd": None, "sold": False}
    lower = title.lower()
    if re.search(r"\b(wts|fs|f\s*[/|-]\s*s|for sale)\b", lower):
        out["wants_to"] = "wts"
    elif re.search(r"\b(wtt|ft)\b", lower):
        out["wants_to"] = "wtt"
    elif re.search(r"\b(wtb|iso|want(?:ing)? to buy)\b", lower):
        out["wants_to"] = "wtb"
    if _SOLD.search(title):
        out["sold"] = True
    prices = _PRICE.findall(title)
    if prices:
        out["price_usd"] = float(prices[0].replace(",", ""))
        out["all_prices"] = [float(p.replace(",", "")) for p in prices]
    return out


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

    def __init__(self, cfg, fetcher):
        super().__init__(cfg, fetcher)
        self._forum_urls: dict[str, str] = {}

    def _forums(self) -> list[str]:
        wanted = self.cfg.forums or list(FORUMS)
        unknown = [f for f in wanted if f not in FORUMS]
        if unknown:
            raise AdapterError(f"calguns: unknown forums {unknown}; valid: {list(FORUMS)}")
        return wanted

    def forum_url(self, forum: str) -> str:
        """Canonical thread-list URL for a forum.

        vB6 slugs can change on a restructure; the RSS feed each forum already
        publishes carries the current one in its <category domain>, so prefer
        that and keep the last answer cached.
        """
        if forum in self._forum_urls:
            return self._forum_urls[forum]
        url: str | None = None
        try:
            feed = self._feed(forum)
            for entry in feed.entries[:1]:
                for tag in entry.get("tags", []):
                    domain = tag.get("scheme", "")
                    if domain.startswith("http") and domain.rstrip("/") != BASE:
                        url = domain.rstrip("/")
                        break
        except AdapterError:
            pass  # fall through to the verified slug table
        if not url and forum in FORUM_SLUGS:
            url = BASE + FORUM_SLUGS[forum]
        if not url:
            raise AdapterError(f"calguns: no known URL for forum '{forum}'")
        self._forum_urls[forum] = url
        return url

    def list_page(self, forum: str, page: int = 1) -> list[ThreadRow]:
        """One page of the thread list (sorted by last activity, stickies skipped)."""
        url = self.forum_url(forum) + (f"/page{page}" if page > 1 else "")
        resp = self.fetcher.get(url)
        if resp.status_code != 200:
            raise AdapterError(f"calguns: {forum} page {page} returned HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.text, "html.parser")
        rows: list[ThreadRow] = []
        for tr in soup.select("table[class*='topic-list-container'] tr.topic-item"):
            if "sticky" in (tr.get("class") or []):
                continue
            a = tr.select_one("a.topic-title[href]")
            if not a:
                continue
            href = a["href"]
            if href.startswith("/"):
                href = BASE + href
            started = tr.select_one(".topic-info .date")
            last = tr.select_one(".cell-lastpost .post-date, .cell-lastpost .date")
            starter = tr.select_one(".topic-info a[data-vbnamecard]")
            counts = _COUNTS.search(tr.select_one(".cell-count").get_text(" ", strip=True) if tr.select_one(".cell-count") else "")
            replies = views = 0
            if counts:
                replies = int(counts.group(1).replace(",", ""))
                views = int(counts.group(2).replace(",", ""))
            rows.append(
                ThreadRow(
                    source=self.name,
                    forum=forum,
                    external_id=_thread_id(href),
                    url=href,
                    title=html.unescape(a.get_text(" ", strip=True)),
                    author=starter.get_text(strip=True) if starter else None,
                    posted_at=parse_vb_date(started.get_text(strip=True) if started else None),
                    last_post_at=parse_vb_date(last.get_text(strip=True) if last else None),
                    replies=replies,
                    views=views,
                )
            )
        if not rows:
            # A page past the end is legitimately empty; a page 1 with no rows
            # means the markup moved. Distinguish by the pagination control.
            if page == 1 and "topic-list-container" not in resp.text:
                raise AdapterError("calguns: thread-list markup changed — no topic-list table found")
            return []
        return rows

    def thread_body(self, url: str) -> tuple[str, str | None]:
        """First-post text (and first content image URL) of a thread."""
        resp = self.fetcher.get(url)
        if resp.status_code != 200:
            raise AdapterError(f"calguns: thread page returned HTTP {resp.status_code}")
        soup = BeautifulSoup(resp.text, "html.parser")
        content = soup.select_one("div.js-post__content-text")
        if content is None:
            raise AdapterError("calguns: thread markup changed — no post content found")
        text = re.sub(r"\s+", " ", content.get_text(" ", strip=True))
        img = content.find("img", src=True)
        image_url = None
        if img is not None:
            src = img["src"]
            if src.startswith("http"):
                image_url = src
            elif src.startswith("/"):
                image_url = BASE + src
        return text[:8000], image_url

    def enrich(self, listing: Listing) -> None:
        if not listing.url:
            return
        body, image_url = self.thread_body(listing.url)
        listing.body = body
        if image_url and not listing.image_url:
            listing.image_url = image_url

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
