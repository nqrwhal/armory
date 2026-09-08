"""Core data model shared by every adapter."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(slots=True)
class Listing:
    source: str
    external_id: str
    url: str
    title: str
    body: str = ""
    price: str | None = None
    category: str | None = None
    location: str | None = None
    author: str | None = None
    posted_at: datetime | None = None
    image_url: str | None = None
    backfilled: bool = False
    matched_keywords: list[str] = field(default_factory=list)
    # Listing intent: "wts" (selling), "wtt" (trading), "wtb" (buying), or None.
    # Set by source-native data, the LLM, or title heuristics — demand-side
    # listings (wtt/wtb) are suppressed for normal keywords by default.
    wants_to: str | None = None
    # LLM layer (None when the LLM is unconfigured or skipped a listing)
    brand: str | None = None
    model: str | None = None
    item_type: str | None = None
    price_usd: float | None = None
    condition: str | None = None
    scam_risk: str | None = None
    scam_reason: str | None = None
    city: str | None = None
    state: str | None = None
    zip: str | None = None
    matched_rules: list[str] = field(default_factory=list)

    @property
    def dedup_key(self) -> tuple[str, str]:
        return (self.source, self.external_id)

    def search_text(self) -> str:
        """Everything keyword matching runs against."""
        parts = [
            self.title,
            self.body,
            self.category or "",
            self.location or "",
            self.author or "",
            self.brand or "",
            self.model or "",
            self.item_type or "",
            self.condition or "",
        ]
        return "\n".join(p for p in parts if p)

    def row(self) -> dict:
        return {
            "source": self.source,
            "external_id": self.external_id,
            "url": self.url,
            "title": self.title,
            "body": self.body,
            "price": self.price,
            "category": self.category,
            "location": self.location,
            "author": self.author,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "image_url": self.image_url,
            "backfilled": int(self.backfilled),
            "matched_keywords": ",".join(self.matched_keywords),
            "brand": self.brand,
            "model": self.model,
            "item_type": self.item_type,
            "price_usd": self.price_usd,
            "condition": self.condition,
            "scam_risk": self.scam_risk,
            "scam_reason": self.scam_reason,
            "city": self.city,
            "state": self.state,
            "zip": self.zip,
            "matched_rules": ",".join(self.matched_rules),
        }


@dataclass(slots=True)
class HealthResult:
    ok: bool
    detail: str


@dataclass(slots=True)
class ThreadRow:
    """One thread/ad from a forum list page (backfill + live ingest).

    Carries the metadata the thread list gives us for free; sources with
    structured ads (caguns CAS) fill price/wants_to/location/body directly,
    while title-convention sources (calguns) leave them for title parsing.
    The full body is fetched lazily for threads that need it.
    """

    source: str
    forum: str
    external_id: str
    url: str
    title: str
    author: str | None = None
    posted_at: datetime | None = None  # thread start
    last_post_at: datetime | None = None  # last activity (bumps refresh this)
    replies: int = 0
    views: int = 0
    # structured extras (optional — source-dependent)
    price: str | None = None
    price_usd: float | None = None
    wants_to: str | None = None
    location: str | None = None  # raw location text ("SoCal / Los Angeles")
    sold: bool = False
    body: str = ""  # short snippet/field summary; full body fetched lazily

    def to_listing(self) -> Listing:
        return Listing(
            source=self.source,
            external_id=self.external_id,
            url=self.url,
            title=self.title,
            author=self.author,
            posted_at=self.posted_at,
            category=self.forum,
        )
