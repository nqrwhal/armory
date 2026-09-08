"""Ingest pipeline shared by the live watch loop and the backfill:

    thread rows → DB upsert → title parsing (already in upsert)
                → geo resolve (title, then body when fetched)
                → lazy body fetch for rows whose title hid price/location
"""

from __future__ import annotations

import random
import time

from .adapters.base import AdapterError
from .db import Db
from .geo import GeoHit, GeoResolver, distance_miles
from .models import ThreadRow


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _apply_geo(db: Db, resolver: GeoResolver, origin: GeoHit, source: str, external_id: str,
               *texts: str) -> bool:
    for text in texts:
        hit = resolver.resolve(text or "")
        if hit:
            db.set_geo(
                source, external_id, hit.lat, hit.lon,
                distance_miles(origin.lat, origin.lon, hit.lat, hit.lon),
                hit.quality, hit.label.split(",")[0], None,
                hit.label if hit.quality == "zip" else None,
            )
            return True
    return False


def _extract_price(body: str) -> float | None:
    from .adapters.calguns import _PRICE

    m = _PRICE.search(body)
    return float(m.group(1).replace(",", "")) if m else None


def ingest_thread_rows(
    adapter,
    db: Db,
    rows: list[ThreadRow],
    resolver: GeoResolver,
    origin: GeoHit,
    body_cap: int = 20,
    throttle: float = 1.5,
    log=_log,
) -> dict:
    """Upsert rows, geo-resolve, and fetch bodies where the title wasn't enough.

    Body fetches are the expensive part (one request per thread), so they're
    capped per call — the watch loop passes a small cap, the backfill a larger
    one — and anything left over stays flagged for a later pass.
    """
    stats = {"rows": len(rows), "new": 0, "changed": 0, "bodies": 0, "geo": 0, "geo_failed": 0}
    if not rows:
        return stats
    result = db.upsert_threads(adapter.name, rows)
    stats["new"] = len(result.new)
    stats["changed"] = len(result.changed)

    # resolve from the title first — free, no requests
    want_body: list[dict] = []
    for item in result.new + result.changed:
        row = db.get_listing(item["source"], item["external_id"])
        if row is None:
            continue
        if _apply_geo(db, resolver, origin, row["source"], row["external_id"], row["title"], row["body"] or ""):
            stats["geo"] += 1
        # title hid the price, or the location didn't resolve → need the body
        if row["needs_enrich"] or row["geo_quality"] is None:
            if not row["body"]:
                want_body.append(row)
            elif row["needs_enrich"] and row["price_usd"] is None:
                price = _extract_price(row["body"])
                if price:
                    db.conn.execute(
                        "UPDATE listings SET price_usd=?, price=?, needs_enrich=0 WHERE source=? AND external_id=?",
                        (price, f"${price:g}", row["source"], row["external_id"]),
                    )
                    db.conn.commit()

    for row in want_body[:body_cap]:
        try:
            body, image_url = adapter.thread_body(row["url"])
        except AdapterError as exc:
            log(f"{adapter.name}: body fetch failed for {row['external_id']}: {exc}")
            continue
        db.set_body(row["source"], row["external_id"], body, image_url)
        stats["bodies"] += 1
        price = None
        if row["price_usd"] is None:
            price = _extract_price(body)
            if price:
                db.conn.execute(
                    "UPDATE listings SET price_usd=?, price=?, needs_enrich=0 WHERE source=? AND external_id=?",
                    (price, f"${price:g}", row["source"], row["external_id"]),
                )
                db.conn.commit()
        if _apply_geo(db, resolver, origin, row["source"], row["external_id"], row["title"], body):
            stats["geo"] += 1
        elif not price:
            stats["geo_failed"] += 1
        if throttle:
            time.sleep(throttle * random.uniform(0.8, 1.2))
    if len(want_body) > body_cap:
        log(f"{adapter.name}: {len(want_body) - body_cap} body fetch(es) deferred to a later pass")
    return stats


def drain_pending_bodies(
    adapter,
    db: Db,
    resolver: GeoResolver,
    origin: GeoHit,
    cap: int,
    throttle: float = 1.5,
    log=_log,
) -> int:
    """Work off the deferred body-fetch backlog (backfill catch-up sessions)."""
    done = 0
    for row in db.pending_enrich(limit=cap):
        try:
            body, image_url = adapter.thread_body(row["url"])
        except AdapterError as exc:
            log(f"{adapter.name}: body fetch failed for {row['external_id']}: {exc}")
            continue
        db.set_body(row["source"], row["external_id"], body, image_url)
        if row["price_usd"] is None:
            price = _extract_price(body)
            if price:
                db.conn.execute(
                    "UPDATE listings SET price_usd=?, price=?, needs_enrich=0 WHERE source=? AND external_id=?",
                    (price, f"${price:g}", row["source"], row["external_id"]),
                )
                db.conn.commit()
        _apply_geo(db, resolver, origin, row["source"], row["external_id"], row["title"], body)
        done += 1
        if throttle:
            time.sleep(throttle * random.uniform(0.8, 1.2))
    return done


class DealContext:
    """Everything the watch loop needs for the deal-hunter pipeline, bundled
    so poll_source can stay adapter-shaped: ingest new forum rows, then drain
    a couple of valuations per cycle."""

    def __init__(self, cfg, db: Db, resolver: GeoResolver, origin: GeoHit, engine, forums: list[str]):
        self.cfg = cfg
        self.db = db
        self.resolver = resolver
        self.origin = origin
        self.engine = engine
        self.forums = forums

    def ingest(self, adapter, log=_log) -> dict | None:
        """Page 1 of each gun forum — catches new/sold/edited threads."""
        if not hasattr(adapter, "list_page"):
            return None
        stats = None
        for forum in self.forums:
            try:
                rows = adapter.list_page(forum, 1)
            except AdapterError as exc:
                log(f"{adapter.name}: {forum} ingest skipped ({exc})")
                continue
            stats = ingest_thread_rows(
                adapter, self.db, rows, self.resolver, self.origin,
                body_cap=8, throttle=self.cfg.backfill.request_interval, log=log,
            )
        return stats

    def save_llm(self, listing) -> None:
        """Persist the poller's classify result onto the DB row, and use the
        LLM's city extraction as a last-chance geo fix for unresolved rows."""
        row = self.db.get_listing(listing.source, listing.external_id)
        if row is None:
            return
        self.db.set_llm_fields(listing.source, listing.external_id, {
            "brand": listing.brand, "model": listing.model, "item_type": listing.item_type,
            "condition": listing.condition, "scam_risk": listing.scam_risk,
            "wants_to": listing.wants_to,
        })
        if listing.city and row["geo_quality"] is None:
            from .geo import distance_miles

            hit = self.resolver.resolve(f"{listing.city}, {listing.state or 'CA'} {listing.zip or ''}")
            if hit:
                self.db.set_geo(
                    listing.source, listing.external_id, hit.lat, hit.lon,
                    distance_miles(self.origin.lat, self.origin.lon, hit.lat, hit.lon),
                    hit.quality, listing.city, listing.state, listing.zip,
                )

    def drain(self, log=_log) -> dict:
        if self.engine is None:
            return {"queued": 0, "valued": 0, "alerted": 0, "errors": 0}
        return self.engine.run(log=log)
