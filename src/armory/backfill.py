"""Resumable backfill: walk forum thread-list pages until the age cutoff.

The list is sorted by last activity, so the walk stops at the first page where
every thread's last activity predates the cutoff — nothing newer can hide
deeper. Progress is saved per page; re-running resumes where it left off and
re-scans the first couple of pages first so threads posted since the last run
are captured even if watch mode wasn't running.
"""

from __future__ import annotations

import random
import time
from datetime import datetime, timedelta, timezone

from .db import Db
from .geo import GeoHit, GeoResolver
from .models import ThreadRow
from .pipeline import drain_pending_bodies, ingest_thread_rows

# Pages re-scanned at the top on resume, absorbing bump drift + new threads.
RESUME_HEAD_PAGES = 2


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _cutoff(days: int) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _page_is_older_than(rows: list[ThreadRow], cutoff: datetime) -> bool:
    dated = [r.last_post_at for r in rows if r.last_post_at is not None]
    if not dated:
        return True  # nothing dated this deep — treat as past the cutoff
    return max(dated) < cutoff


def run_backfill(
    adapter,
    db: Db,
    resolver: GeoResolver,
    origin: GeoHit,
    forums: list[str],
    days: int = 90,
    pages_limit: int = 0,          # 0 = no cap
    body_cap_per_page: int = 60,   # ~half of calguns titles hide the price
    throttle: float = 1.5,
    log=_log,
) -> dict:
    cutoff = _cutoff(days)
    stats = {"pages": 0, "rows": 0, "new": 0, "changed": 0, "bodies": 0, "geo": 0, "done_forums": []}
    for forum in forums:
        progress = db.backfill_progress(forum)
        pages_done = progress["pages_done"] or 0
        if progress["done"]:
            log(f"{forum}: already backfilled — re-scanning the head pages for new arrivals")

        # fresh head pages first (new threads + drift), then the deep walk
        head_pages = list(range(1, RESUME_HEAD_PAGES + 1))
        next_deep = max(RESUME_HEAD_PAGES + 1, pages_done + 1)
        queue: list[int] = head_pages if progress["done"] else head_pages + [next_deep]
        next_deep += 1  # the queue already owns that page
        qi = 0

        while qi < len(queue) or not progress["done"]:
            if pages_limit and stats["pages"] >= pages_limit:
                log(f"{forum}: page limit reached — progress saved, resume later")
                break
            if qi < len(queue):
                page = queue[qi]
                qi += 1
            else:
                page = next_deep
                next_deep += 1

            rows = adapter.list_page(forum, page)
            stats["pages"] += 1
            if not rows:
                log(f"{forum}: page {page} empty — end of forum")
                db.set_backfill_progress(forum, max(pages_done, page), True)
                stats["done_forums"].append(forum)
                break
            page_stats = ingest_thread_rows(
                adapter, db, rows, resolver, origin,
                body_cap=body_cap_per_page, throttle=throttle, log=log,
            )
            for key in ("rows", "new", "changed", "bodies", "geo"):
                stats[key] += page_stats[key]
            if _page_is_older_than(rows, cutoff):
                log(f"{forum}: page {page} fully older than {days}d — cutoff reached")
                db.set_backfill_progress(forum, max(pages_done, page), True)
                stats["done_forums"].append(forum)
                break
            pages_done = max(pages_done, page)
            db.set_backfill_progress(forum, pages_done, False)
            log(
                f"{forum}: page {page} — {page_stats['new']} new, {page_stats['changed']} changed, "
                f"{page_stats['bodies']} bodies ({stats['rows']} rows total)"
            )
            time.sleep(throttle * random.uniform(0.8, 1.2))
    return stats


def run_enrich_backlog(
    adapter,
    db: Db,
    resolver: GeoResolver,
    origin: GeoHit,
    cap: int = 500,
    throttle: float = 1.5,
    log=_log,
) -> int:
    """Fetch bodies for rows the page walk deferred (price/location still hidden)."""
    return drain_pending_bodies(adapter, db, resolver, origin, cap=cap, throttle=throttle, log=log)
