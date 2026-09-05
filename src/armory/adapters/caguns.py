"""caguns.net — XenForo 2 + CAS classifieds (Cloudflare + login wall; needs your cookies)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..models import HealthResult, Listing
from .base import AdapterError, SourceAdapter

BASE = "https://caguns.net"

# Simple alias paths (verified) plus category paths discovered during recon.
CATEGORY_PATHS: dict[str, str] = {
    "firearms": "/firearms",
    "longguns": "/longguns",
    "ammo": "/ammo",
    "reloading": "/reloading",
    "wtb": "/wtb",
    "shotguns": "/classifieds/categories/private-shotgun-listings.13/",
    "non_firearm": "/classifieds/categories/private-non-firearm-related-listings.11/",
    "trade_pif": "/classifieds/categories/want-to-trade-pay-it-forward.12/",
    "barrels_uppers": "/classifieds/categories/barrels-complete-slides-uppers.14/",
    "curio_relic": "/classifieds/categories/curio-relic.15/",
}

_AD_ID_FROM_CLASS = re.compile(r"js-adListItem-(\d+)")
_AD_URL = re.compile(r"^/classifieds/[a-z0-9-]+\.(\d+)/?$")
_PRICE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")


def _classify_block(html: str) -> str | None:
    """Return an error reason if the page is not a usable classifieds listing."""
    low = html.lower()
    if "just a moment" in low or "cf-challenge" in low or "challenge-platform" in html:
        return "Cloudflare challenge (cookies missing/stale or IP flagged)"
    if "you must be logged-in" in low or "log in or register" in low.replace("’", "'"):
        if "structItem--ad" not in html:
            return "login wall — update CAGUNS_COOKIES in .env (see armory setup)"
    return None


class CagunsAdapter(SourceAdapter):
    name = "caguns"

    def _categories(self) -> list[str]:
        cats = self.cfg.categories or list(CATEGORY_PATHS)[:5]
        unknown = [c for c in cats if c not in CATEGORY_PATHS]
        if unknown:
            raise AdapterError(f"caguns: unknown categories {unknown}; valid: {list(CATEGORY_PATHS)}")
        return cats

    def _page(self, category: str, page: int) -> str:
        resp = self.fetcher.get(BASE + CATEGORY_PATHS[category], params={"page": page} if page > 1 else None)
        if resp.status_code in (403, 503):
            raise AdapterError("caguns: HTTP %d — %s" % (resp.status_code, _classify_block(resp.text) or "blocked"))
        if resp.status_code != 200:
            raise AdapterError(f"caguns: HTTP {resp.status_code}")
        reason = _classify_block(resp.text)
        if reason:
            raise AdapterError(f"caguns: {reason}")
        return resp.text

    def parse_page(self, html: str) -> list[Listing]:
        soup = BeautifulSoup(html, "html.parser")
        listings: list[Listing] = []
        for ad in soup.select("div.structItem--ad"):
            classes = " ".join(ad.get("class", []))
            id_match = _AD_ID_FROM_CLASS.search(classes)
            title_link = None
            for a in ad.select(".structItem-title a[href]"):
                if _AD_URL.match(a["href"].split("?")[0]):
                    title_link = a
                    break
            if not id_match or title_link is None:
                continue
            ad_id = id_match.group(1)
            url = BASE + title_link["href"].split("?")[0]
            title = title_link.get_text(" ", strip=True)
            if not title:
                continue
            price_el = ad.find("span", string=_PRICE)
            price = price_el.get_text(strip=True) if price_el else None
            fields: dict[str, str] = {}
            for row in ad.select("dl"):
                dt = row.find("dt")
                dd = row.find("dd")
                if dt and dd:
                    fields[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
            location = " / ".join(f for f in (fields.get("Region"), fields.get("Sub-Region")) if f) or None
            posted_at = None
            first_time = ad.find("time", attrs={"data-timestamp": True})
            if first_time:
                posted_at = datetime.fromtimestamp(int(first_time["data-timestamp"]), tz=timezone.utc)
            category_link = ad.select_one("a[href*='/classifieds/categories/']")
            prefix_link = ad.select_one("a[href*='/classifieds/?type=']")
            prefix = prefix_link.get_text(" ", strip=True).upper() if prefix_link else ""
            wants_to = {"WTS": "wts", "WTB": "wtb", "WTT": "wtt"}.get(prefix)
            snippet_el = ad.select_one(".structItem-adDescription")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            body_bits = ([snippet] if snippet else []) + [
                f"{k}: {v}" for k, v in fields.items() if k
            ]
            img = ad.find("img", src=re.compile(r"^https?://"))
            listings.append(
                Listing(
                    source=self.name,
                    external_id=ad_id,
                    url=url,
                    title=title,
                    body="; ".join(body_bits)[:2000],
                    price=price,
                    category=category_link.get_text(" ", strip=True) if category_link else None,
                    location=location,
                    author=ad.get("data-author"),
                    posted_at=posted_at,
                    image_url=img["src"] if img else None,
                    wants_to=wants_to,
                )
            )
        return listings

    def poll(self, pages: int = 1) -> list[Listing]:
        listings: list[Listing] = []
        for cat in self._categories():
            for page in range(1, max(1, pages) + 1):
                listings.extend(self.parse_page(self._page(cat, page)))
        return listings

    def health(self) -> HealthResult:
        try:
            resp = self.fetcher.get(BASE + "/classifieds/")
        except Exception as exc:  # noqa: BLE001
            return HealthResult(False, f"network error: {exc}")
        if resp.status_code != 200:
            return HealthResult(False, f"HTTP {resp.status_code} — {_classify_block(resp.text) or 'blocked'}")
        reason = _classify_block(resp.text)
        if reason:
            return HealthResult(False, reason)
        if "structItem--ad" in resp.text:
            return HealthResult(True, "classifieds OK")
        return HealthResult(False, "page loaded but no ad blocks found")
