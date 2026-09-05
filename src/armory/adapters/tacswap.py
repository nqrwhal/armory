"""tacswap.com — server-rendered Next.js category pages (public)."""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta

from bs4 import BeautifulSoup

from ..models import HealthResult, Listing, utcnow
from .base import AdapterError, SearchUnsupported, SourceAdapter

BASE = "https://tacswap.com"

CATEGORIES = [
    "firearm",
    "suppressors",
    "firearm_parts",
    "firearm_accessories",
    "nightvision",
    "ammo",
    "apparel",
    "knives",
    "optics",
    "tactical_kit",
    "services",
    "misc",
]

_POST_ID = re.compile(r"^[0-9a-f]{24}$")
_PRICE = re.compile(r"^\$[\d,]+(?:\.\d{2})?$")
_LOCATION = re.compile(r"^[A-Za-z .]+,\s*[A-Z]{2}\s*\d{5}(?:-\d{4})?$")

# Search runs through a Next.js server action (discovered 2026-09 by replaying
# the SPA's XHR). The action id rotates whenever tacswap deploys, but it ships
# in the RSC payload of every category page, so it's re-discovered automatically
# — the discovery GET doubles as the GAESA edge-cookie prime.
_ACTION_REF = re.compile(r'\\?"id\\?":\\?"([0-9a-f]{40,})\\?",\\?"bound')
_search_action_id: str | None = None

_FLIGHT_LISTING_START = re.compile(r'\{"id":"([0-9a-f]{24})","authorUsername":')
_REL_UNITS = {
    "sec": "seconds",
    "min": "minutes",
    "hour": "hours",
    "day": "days",
    "week": "weeks",
    "month": "days",  # month: 30 days
    "yr": "days",     # year: 365 days, multiplied below
}
_REL_MULT = {"month": 30, "yr": 365}


def _parse_relative(text: str):
    m = re.match(r"^(\d+)\s*(sec|secs|min|mins|hour|hours|day|days|week|weeks|month|months|yr|yrs)\b", text.lower())
    if not m:
        return None
    n = int(m.group(1))
    unit = m.group(2).rstrip("s")
    if unit in _REL_MULT:
        return utcnow() - timedelta(days=n * _REL_MULT[unit])
    return utcnow() - timedelta(**{_REL_UNITS[unit]: n})


def _extract_json_object(text: str, start: int) -> tuple[dict | None, int]:
    """Brace-match one JSON object out of a React Flight stream (string-aware)."""
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1]), i + 1
                except json.JSONDecodeError:
                    return None, i + 1
    return None, len(text)


def parse_flight_listings(text: str) -> list[dict]:
    """Pull structured listing objects out of a Next.js flight payload."""
    out: dict[str, dict] = {}
    pos = 0
    while True:
        m = _FLIGHT_LISTING_START.search(text, pos)
        if not m:
            break
        obj, end = _extract_json_object(text, m.start())
        pos = end
        if obj and obj.get("itemTitle"):
            out[obj["id"]] = obj
    return list(out.values())


def _flight_listing_to_model(obj: dict) -> Listing:
    places = (obj.get("location") or {}).get("places") or {}
    location = None
    if places.get("placeName"):
        bits = [places["placeName"]]
        if places.get("stateAbbreviation"):
            bits.append(places["stateAbbreviation"])
        if obj.get("zipCode"):
            bits.append(obj["zipCode"])
        location = " ".join(bits)
    image_url = None
    assets = obj.get("assets") or []
    if assets and assets[0].get("public_id"):
        image_url = f"https://img.tacswap.com/{assets[0]['public_id']}?p=thumb&fit=cover"
    price = None
    if obj.get("itemValue") is not None:
        price = "${:,}".format(obj["itemValue"])
    posted = None
    for key in ("createdAt", "dateAdded", "postedAt", "lastEdited", "lastBumped"):
        if obj.get(key):
            try:
                posted = datetime.fromisoformat(obj[key].replace("Z", "+00:00"))
            except ValueError:
                pass
            break
    return Listing(
        source="tacswap",
        external_id=obj["id"],
        url=f"https://tacswap.com/post/{obj['id']}",
        title=obj["itemTitle"],
        body=re.sub(r"\s+", " ", obj.get("description") or "")[:4000],
        price=price,
        category=obj.get("category"),
        location=location,
        author=obj.get("authorUsername"),
        posted_at=posted,
        image_url=image_url,
        wants_to=obj.get("postType") if obj.get("postType") in ("wts", "wtt", "wtb") else None,
    )


class TacswapAdapter(SourceAdapter):
    name = "tacswap"

    def _categories(self) -> list[str]:
        cats = self.cfg.categories or CATEGORIES
        unknown = [c for c in cats if c not in CATEGORIES]
        if unknown:
            raise AdapterError(f"tacswap: unknown categories {unknown}; valid: {CATEGORIES}")
        return cats

    def _page(self, category: str, page: int) -> str:
        resp = self.fetcher.get(f"{BASE}/{category}", params={"page": page} if page > 1 else None)
        if resp.status_code != 200:
            raise AdapterError(f"tacswap: /{category}?page={page} returned HTTP {resp.status_code}")
        return resp.text

    def parse_page(self, html: str, category: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []
        for card in soup.find_all("div", attrs={"data-post-id": True}):
            post_id = card["data-post-id"]
            if not _POST_ID.match(post_id):
                continue
            title = ""
            image_url = None
            for a in card.find_all("a", href=True):
                if not a["href"].startswith("/post/"):
                    continue
                text = a.get_text(" ", strip=True)
                if text and not title:
                    title = text
                img = a.find("img", src=True)
                if img and not image_url and img["src"].startswith("http"):
                    image_url = img["src"]
                    if img.get("alt") and not title:
                        title = img["alt"].strip()
            if not title:
                continue
            author = None
            for user_link in card.find_all("a", href=re.compile(r"^/user/")):
                # avatar link holds only an <img>; the username link has text
                text = user_link.get_text(" ", strip=True)
                if text:
                    author = text.split("(")[0].strip() or None
                    break
            texts = [t.strip() for t in card.stripped_strings]
            price = next((t for t in texts if _PRICE.match(t)), None)
            location = next((t for t in texts if _LOCATION.match(t)), None)
            posted_at = None
            for t in texts:
                posted_at = _parse_relative(t)
                if posted_at:
                    break
            listings.append(
                Listing(
                    source=self.name,
                    external_id=post_id,
                    url=f"{BASE}/post/{post_id}",
                    title=title,
                    price=price,
                    category=category,
                    location=location,
                    author=author,
                    posted_at=posted_at,
                    image_url=image_url,
                )
            )
        return listings

    def poll(self, pages: int = 1) -> list[Listing]:
        listings: list[Listing] = []
        for cat in self._categories():
            for page in range(1, max(1, pages) + 1):
                listings.extend(self.parse_page(self._page(cat, page), cat))
        return listings

    def _discover_action_id(self) -> str:
        """Pull the current search action id from a category page's RSC payload."""
        resp = self.fetcher.get(f"{BASE}/firearm_accessories")
        m = _ACTION_REF.search(resp.text)
        if not m:
            raise SearchUnsupported("tacswap: search action id not found in page payload (site changed)")
        return m.group(1)

    def search(self, query: str, limit: int = 25) -> list[Listing]:
        global _search_action_id
        body = [
            {
                "pageParam": {
                    "category": "",
                    "searchQuery": {
                        "source": "",
                        "type": "",
                        "q": query,
                        "sortBy": "relevancy",
                        "searchPostType": [],
                        "status": [],
                        "state": "",
                        "zipcode": "",
                        "distance": "25",
                    },
                }
            }
        ]
        # The edge (GAESA token) rejects server-action POSTs from cookieless
        # clients; the discovery/refresh GET also primes the cookie jar.
        for attempt in range(3):
            if _search_action_id is None or attempt > 0:
                _search_action_id = self._discover_action_id()
            resp = self.fetcher.post(
                f"{BASE}/?q={query}",
                body,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "text/x-component",
                    "next-action": _search_action_id,
                },
            )
            if resp.status_code == 404 or "Server action not found" in resp.text:
                continue  # id rotated mid-flight or edge being fussy: rediscover
            if resp.status_code != 200:
                raise AdapterError(f"tacswap: search returned HTTP {resp.status_code}")
            listings = [_flight_listing_to_model(o) for o in parse_flight_listings(resp.text)]
            if not listings and resp.text.lstrip().startswith('0:{"a"'):
                continue  # occasional router-stub response; retry
            listings.sort(key=lambda l: l.posted_at or utcnow(), reverse=True)
            return listings[:limit]
        raise SearchUnsupported("tacswap search unavailable after action-id rediscovery")

    def health(self) -> HealthResult:
        try:
            html = self._page("ammo", 1)
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"network error: {exc}")
        if "data-post-id" in html:
            return HealthResult(True, "category pages OK")
        return HealthResult(False, "page loaded but no listing cards found (layout change?)")

    def enrich(self, listing: Listing) -> None:
        """Fetch the post page for its full description (cards are title-only)."""
        if listing.body:
            return
        try:
            resp = self.fetcher.get(listing.url)
        except Exception:  # noqa: BLE001 — enrichment is best-effort
            return
        if resp.status_code != 200:
            return
        soup = BeautifulSoup(resp.text, "html.parser")
        paragraphs = [p.get_text(" ", strip=True) for p in soup.select("p.break-words")]
        body = "\n".join(p for p in paragraphs if p)
        if body:
            listing.body = re.sub(r"[ \t]+", " ", body)[:4000]
