"""caguns.net — XenForo 2 + CAS classifieds (Cloudflare + login wall; needs your cookies)."""

from __future__ import annotations

import re
from datetime import datetime, timezone

from bs4 import BeautifulSoup

from ..models import HealthResult, Listing, ThreadRow
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
    "trade_pif": "/classifieds/categories/private-want-to-trade-pay-it-forward.12/",
    "barrels_uppers": "/classifieds/categories/barrels-complete-slides-uppers.14/",
    "curio_relic": "/classifieds/categories/private-curio-relic.15/",
}

_AD_ID_FROM_CLASS = re.compile(r"js-adListItem-(\d+)")
_AD_URL = re.compile(r"^/classifieds/[a-z0-9-]+\.(\d+)/?$")
_AD_TYPE_FROM_CLASS = re.compile(r"is-ad-type-([a-z_]+)")
_PRICE = re.compile(r"\$\d[\d,]*(?:\.\d{2})?")
_PRICE_NUM = re.compile(r"\$([\d,]+(?:\.\d{2})?)")
_SOLD = re.compile(r"\b(sold|spf)\b", re.IGNORECASE)
# live pages carry the ad type as a CSS class (?type= links are long gone)
_AD_TYPE_TO_WANTS = {
    "for_sale": "wts",
    "for_trade": "wtt",
    "wanted_to_buy": "wtb",
    "wanted": "wtb",
}
_PREFIX_TO_WANTS = {"WTS": "wts", "WTB": "wtb", "WTT": "wtt"}
# ad fields worth carrying into the body summary for the valuation LLM
_PERSIST_FIELDS = ("Region", "Sub-Region", "Caliber", "Roster", "FFL Required Transfer", "Shipping", "Open to Trades")


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

    # --- deal pipeline: ThreadRows + full ad bodies ---

    @staticmethod
    def _fields(ad) -> dict[str, str]:
        fields: dict[str, str] = {}
        for row in ad.select("dl"):
            dt = row.find("dt")
            dd = row.find("dd")
            if dt and dd:
                fields[dt.get_text(" ", strip=True)] = dd.get_text(" ", strip=True)
        return fields

    def list_page(self, category: str, page: int = 1) -> list[ThreadRow]:
        """One page of a category as ThreadRows (structured CAS fields included)."""
        soup = BeautifulSoup(self._page(category, page), "html.parser")
        rows: list[ThreadRow] = []
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
            title = title_link.get_text(" ", strip=True)
            if not title:
                continue
            fields = self._fields(ad)
            price_el = ad.find("span", string=_PRICE)
            price = price_el.get_text(strip=True) if price_el else None
            price_usd = None
            if price:
                m = _PRICE_NUM.search(price)
                if m:
                    price_usd = float(m.group(1).replace(",", ""))
            # posted = first timestamp; the Updated field's own <time> = activity.
            # Never fall back to times[1] — that's usually Expires (months out).
            times = ad.find_all("time", attrs={"data-timestamp": True})
            posted_at = datetime.fromtimestamp(int(times[0]["data-timestamp"]), tz=timezone.utc) if times else None
            updated_dt = ad.find("dt", string=re.compile(r"^\s*Updated\s*$"))
            updated_el = updated_dt.find_next("time", attrs={"data-timestamp": True}) if updated_dt else None
            last_post_at = (
                datetime.fromtimestamp(int(updated_el["data-timestamp"]), tz=timezone.utc)
                if updated_el else posted_at
            )
            comments = fields.get("Comments", "0").split()[0].replace(",", "")
            views = fields.get("Views", "0").split()[0].replace(",", "")
            snippet_el = ad.select_one(".structItem-adDescription")
            snippet = snippet_el.get_text(" ", strip=True) if snippet_el else ""
            body_bits = [snippet] + [f"{k}: {fields[k]}" for k in _PERSIST_FIELDS if fields.get(k)]
            prefix_link = ad.select_one("a[href*='/classifieds/?type=']")
            wants_to = None
            if prefix_link:
                wants_to = _PREFIX_TO_WANTS.get(prefix_link.get_text(" ", strip=True).upper())
            else:
                type_match = _AD_TYPE_FROM_CLASS.search(classes)
                wants_to = _AD_TYPE_TO_WANTS.get(type_match.group(1)) if type_match else None
            location = " / ".join(f for f in (fields.get("Region"), fields.get("Sub-Region")) if f) or None
            sold = bool(_SOLD.search(title)) or "sold" in (fields.get("Status") or "").lower()
            rows.append(
                ThreadRow(
                    source=self.name,
                    forum=category,
                    external_id=id_match.group(1),
                    url=BASE + title_link["href"].split("?")[0],
                    title=title,
                    author=ad.get("data-author"),
                    posted_at=posted_at,
                    last_post_at=last_post_at,
                    replies=int(comments) if comments.isdigit() else 0,
                    views=int(views) if views.isdigit() else 0,
                    price=price,
                    price_usd=price_usd,
                    wants_to=wants_to,
                    location=location,
                    sold=sold,
                    body="; ".join(b for b in body_bits if b)[:1500],
                )
            )
        if not rows and page == 1:
            # _page already rejects login walls/challenges; empty page 1 here
            # means the ad-block markup itself changed
            raise AdapterError("caguns: classifieds markup changed — no ad blocks parsed")
        return rows

    def thread_body(self, url: str) -> tuple[str, str | None]:
        """Full ad description (first article.adBody-main) + first image."""
        resp = self.fetcher.get(url)
        if resp.status_code != 200:
            raise AdapterError(f"caguns: ad page returned HTTP {resp.status_code}")
        reason = _classify_block(resp.text)
        if reason:
            raise AdapterError(f"caguns: {reason}")
        soup = BeautifulSoup(resp.text, "html.parser")
        article = soup.select_one("article.adBody-main") or soup.select_one("article .message-body")
        if article is None:
            raise AdapterError("caguns: ad markup changed — no description block found")
        text = re.sub(r"\s+", " ", article.get_text(" ", strip=True))
        img = article.find("img", src=re.compile(r"^https?://"))
        return text[:8000], img["src"] if img else None

    def enrich(self, listing: Listing) -> None:
        if not listing.url:
            return
        body, image_url = self.thread_body(listing.url)
        listing.body = body
        if image_url and not listing.image_url:
            listing.image_url = image_url

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
