"""Polling engine: state-based new-listing detection, matching, alerts, watch loop."""

from __future__ import annotations

import random
import sys
import time
from datetime import timedelta

from .adapters.base import AdapterError, SourceAdapter
from .alerts import Alerters
from .llm import LLMClient, LLMError, apply_result
from .matching import load_matchers, match_listing
from .models import Listing
from .state import StateStore, parse_iso, utcnow_iso

# New listings each get one enrich request (post-page fetch) before matching;
# capped per source per cycle so a busy hour doesn't turn into a crawl.
ENRICH_CAP = 12

# A listing older than this before our last check is treated as carry-over
# from the feed page (or a bump), not something worth re-alerting on.
CUTOFF_SLACK = timedelta(minutes=15)


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _select_new(state: StateStore, source: str, listings: list[Listing]) -> tuple[list[Listing], bool]:
    """Returns (new listings, first_run)."""
    src = state.source(source)
    if state.is_first_run(source):
        return [], True  # caller seeds via update_source with everything seen
    seen = set(src.recent_ids)
    cutoff = parse_iso(src.last_check)
    new: list[Listing] = []
    for listing in listings:
        if listing.external_id in seen:
            continue
        if cutoff and listing.posted_at and listing.posted_at < cutoff - CUTOFF_SLACK:
            continue  # old enough that we presumably alerted (or ignored) it already
        new.append(listing)
    return new, False


def poll_source(
    state: StateStore,
    adapter: SourceAdapter,
    alerters: Alerters,
    llm: LLMClient | None = None,
    max_alerts: int = 6,
    pages: int = 1,
    deal_ctx=None,
) -> dict[str, int]:
    """Poll one source: detect new listings, enrich + classify, alert on matches.

    With a DealContext, also ingests forum rows into the DB and drains a
    couple of valuations — the continual-processing side of the deal hunter.

    Returns {"polled": n, "new": n, "matched": n, "suppressed": n, "alerts_failed": n}.
    """
    listings: list[Listing] = adapter.poll(pages=pages)
    all_ids = [l.external_id for l in listings]
    now = utcnow_iso()

    new, first_run = _select_new(state, adapter.name, listings)
    if first_run:
        state.update_source(adapter.name, all_ids, now)
        state.save()
        _log(f"{adapter.name}: first run — seeded {len(all_ids)} listing id(s), no alerts")
        return {"polled": len(listings), "new": 0, "reseen": 0, "matched": 0, "suppressed": 0, "alerts_failed": 0}

    # bumped listings re-detected after the ring rotates out: never re-alert,
    # and don't waste enrich/LLM calls on them either
    fresh = [l for l in new if not state.has_alerted(adapter.name, l.external_id)]
    reseen = len(new) - len(fresh)

    enriched = 0
    for listing in fresh:
        if not listing.body and enriched < ENRICH_CAP:
            try:
                adapter.enrich(listing)
                enriched += 1
            except Exception:  # noqa: BLE001 — enrichment is best-effort
                pass

    if fresh and llm is not None and llm.configured:
        rules = state.rules()
        try:
            results = llm.classify_batch(fresh, rules)
        except LLMError as exc:
            _log(f"{adapter.name}: LLM classify skipped ({exc})")
            results = {}
        for listing in fresh:
            result = results.get(listing.external_id)
            if result:
                apply_result(listing, result)
                if deal_ctx is not None:
                    try:
                        deal_ctx.save_llm(listing)
                    except Exception:  # noqa: BLE001 — persistence is best-effort
                        pass

    matchers = load_matchers(state)
    matched: list[Listing] = []
    for listing in fresh:
        hits = match_listing(listing, matchers) if matchers else []
        if hits:
            listing.matched_keywords = hits
        if hits or listing.matched_rules:
            matched.append(listing)

    # overflow control: cap alerts per cycle, summarize the rest
    suppressed = max(0, len(matched) - max_alerts)
    alert_list = matched[:max_alerts]
    note = f"+{suppressed} more suppressed this cycle" if suppressed else None
    alert_failures = 0
    if alert_list:
        for err in alerters.send_all(alert_list, note=note):
            _log(f"! alert error: {err}")
            alert_failures += 1
    if matched:
        state.mark_alerted(adapter.name, [l.external_id for l in matched])

    state.update_source(adapter.name, all_ids + [l.external_id for l in new], now)
    state.save()

    if deal_ctx is not None:
        try:
            ingest_stats = deal_ctx.ingest(adapter)
            if ingest_stats and (ingest_stats["new"] or ingest_stats["changed"]):
                _log(
                    f"{adapter.name}: ingested {ingest_stats['new']} new / "
                    f"{ingest_stats['changed']} changed forum rows "
                    f"({ingest_stats['geo']} geo-located, {ingest_stats['bodies']} bodies)"
                )
        except Exception as exc:  # noqa: BLE001 — deal pipeline must not kill polling
            _log(f"{adapter.name}: deal-pipeline ingest error: {exc}")
        try:
            deal_ctx.drain()
        except Exception as exc:  # noqa: BLE001
            _log(f"{adapter.name}: valuation drain error: {exc}")

    return {
        "polled": len(listings),
        "new": len(new),
        "reseen": reseen,
        "matched": len(matched),
        "suppressed": suppressed,
        "alerts_failed": alert_failures,
    }


def run_poll_cycle(
    state: StateStore,
    adapters: dict[str, SourceAdapter],
    alerters: Alerters,
    llm: LLMClient | None = None,
    max_alerts: int = 6,
    only: str | None = None,
    pages: int = 1,
    deal_ctx=None,
) -> bool:
    """Poll sources once. Returns True if any source completed."""
    any_ok = False
    for name, adapter in adapters.items():
        if only and name != only:
            continue
        try:
            stats = poll_source(state, adapter, alerters, llm=llm, max_alerts=max_alerts,
                                pages=pages, deal_ctx=deal_ctx)
        except AdapterError as exc:
            _log(f"{name}: {exc}")
            continue
        except Exception as exc:  # noqa: BLE001 — keep the loop alive
            _log(f"{name}: unexpected error: {exc}")
            continue
        any_ok = True
        if stats["new"] or stats["matched"]:
            msg = f"{name}: {stats['polled']} polled, {stats['new']} new"
            if stats["reseen"]:
                msg += f" ({stats['reseen']} already-alerted bumps)"
            if stats["matched"]:
                msg += f", {stats['matched']} match(es)"
            if stats["suppressed"]:
                msg += f", {stats['suppressed']} suppressed"
            _log(msg)
    return any_ok


def watch(
    state: StateStore,
    adapters: dict[str, SourceAdapter],
    alerters: Alerters,
    intervals: dict[str, int],
    llm: LLMClient | None = None,
    max_alerts: int = 6,
    deal_ctx=None,
) -> None:
    """Poll forever, each source on its own interval (±20% jitter)."""
    _log(
        f"watching {len(adapters)} source(s): "
        + ", ".join(f"{name} ({intervals.get(name, 300)}s)" for name in adapters)
        + f"; alerts: {alerters.enabled() or 'none'}"
        + ("; llm: on" if llm and llm.configured else "; llm: off (keywords only)")
        + ("; deals: on" if deal_ctx is not None else "")
    )
    next_at = {name: 0.0 for name in adapters}
    try:
        while True:
            now = time.time()
            for name in adapters:
                if now >= next_at[name]:
                    # reload so CLI keyword/rule edits land in the live watcher
                    # instead of being clobbered by its next save
                    state = StateStore(state.path)
                    run_poll_cycle(state, adapters, alerters, llm=llm, max_alerts=max_alerts,
                                   only=name, deal_ctx=deal_ctx)
                    jitter = random.uniform(0.8, 1.2)
                    next_at[name] = now + intervals.get(name, 300) * jitter
            now = time.time()
            wake = min(next_at.values()) - now if next_at else 60
            time.sleep(min(max(wake, 5), 60))
    except KeyboardInterrupt:
        _log("stopped")
        sys.exit(0)
