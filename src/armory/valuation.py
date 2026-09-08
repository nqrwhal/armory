"""Deal-hunter valuation: price each in-radius listing with GLM + web search.

Pipeline per listing:
  1. identify make/model first if the poller's classify pass never ran on it
     (backfilled rows) — one cheap batched call
  2. deterministic CA-roster lookup + local comps from our own DB
  3. GLM-5.3-Flash with thinking ON and a web_search tool (executed through
     the z.ai MCP server) prices the gun + attachments and scores the deal
  4. score at/above the threshold and a good verdict → Discord/iMessage alert

Latency is explicitly not a concern; throughput and politeness are.
"""

from __future__ import annotations

import json
import time

from .config import ValuationConfig
from .db import Db
from .llm import LLMClient, LLMError, apply_result, extract_json
from .models import Listing
from .roster import RosterIndex

WEB_SEARCH_TOOL = {
    "name": "web_search",
    "description": (
        "Search the web. Use for current market prices (recent sold/asking on "
        "GunBroker, guns.com, dealer sites like PSA/Sportsman's), gun model "
        "specs/identification, and California roster status. 1-3 focused "
        "queries are usually enough."
    ),
    "input_schema": {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "focused search query"}},
        "required": ["query"],
    },
}

VALUATION_SYSTEM = """You are a firearms market analyst for a personal deal-finder in California.

You receive ONE marketplace listing and context (local comps, CA roster lookup
result, distance from the buyer). Produce a rigorous price opinion.

Method:
1. Identify the exact make/model/variant. Itemize only extras INCLUDED in the
   sale (optics, lights, extra mags, ammo that comes with it, cases) with
   individual resale values — "500 rounds fired" is wear, not ammunition
   included.
2. Establish market value. Search the web for current asking/sold prices
   (GunBroker sold listings, guns.com, major dealers). Weight local comps when
   given. The value range is for the package: base gun + attachments, adjusted
   for condition and CA market reality.
3. California roster: the context includes a deterministic DOJ-roster lookup
   hint ('on'/'off'/'unknown'/'n/a' — n/a means long gun). Reconcile it with
   what you know / find. OFF-roster handguns carry a CA premium (typically
   +20-100% over national used value — judge it for this model); ON-roster and
   long guns get none.
4. Score the deal: asking vs market mid, attachments included, condition,
   scarcity/off-roster correctness, scam/red flags. Rough scale:
   asking <=60% mid → 90-100 | 60-75% → 80-90 | 75-85% → 65-80 |
   85-95% → 45-65 | 95-110% → 20-45 | >110% → 0-20.
   Cap 'great'/'good' verdicts when the listing smells like a scam; say so.
5. MULTI-ITEM PRICING — read the listing like a buyer, not a regex. Many
   listings sell several guns in one thread. If the stated price is PER ITEM
   ("$X each", "apiece", "per gun", or one price beside multiple firearms
   with no "for both/for all"), then the total asking price is price ×
   quantity. ALWAYS score against the TOTAL, never the per-item number, and
   set asking_price_is_per_item=true with asking_total filled in. Never
   describe a per-item price as buying the whole lot. If the pricing is
   genuinely ambiguous, say so in caveats, set asking_total to your best
   conservative reading, and shade the deal score down — a mis-read bundle
   is worse than a missed marginal deal.

Return STRICT JSON only:
{"make": str|null, "model": str|null, "variant": str|null,
 "confidence": "high|medium|low",
 "attachments": [{"item": str, "est_value_usd": number}],
 "market_low": number, "market_mid": number, "market_high": number,
 "value_basis": "1-2 sentences: what the range is built on",
 "ca_roster": "on|off|unknown|n/a",
 "off_roster_premium_pct": number|null,
 "asking_price_is_per_item": true|false,
 "asking_total": number|null,
 "deal_score": 0-100,
 "verdict": "great|good|fair|poor|overpriced|unclear",
 "summary": "2-3 sentences a buyer would actually want to read",
 "caveats": str|null,
 "scam_risk": "low|medium|high",
 "sources": [url strings]}"""


def _log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def _f(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


class ValuationEngine:
    def __init__(
        self,
        db: Db,
        llm: LLMClient,
        cfg: ValuationConfig,
        search=None,          # ZaiSearch or None → no web tool
        roster: RosterIndex | None = None,
        alerters=None,
        radius_miles: float = 100.0,
        adapters: dict | None = None,   # for lazy full-body fetches per source
    ):
        self.db = db
        self.llm = llm
        self.cfg = cfg
        self.radius = radius_miles
        self.search = search if (search and search.configured and cfg.web_search) else None
        self.roster = roster
        self.alerters = alerters
        self.adapters = adapters or {}
        # source → forum names that hold handguns (roster applies to these)
        self.handgun_forums: dict[str, set[str]] = {
            "calguns": {"handguns"},
            "caguns": {"firearms"},
        }

    # --- context building ---

    def _ensure_identity(self, listing: dict) -> dict | None:
        """Backfilled rows never met the poller's classify pass — fill make/model.
        Returns LLM fields for the main thread to persist (or None)."""
        if listing.get("brand") and listing.get("model"):
            return None
        probe = Listing(
            source=listing["source"], external_id=listing["external_id"], url=listing["url"],
            title=listing["title"] or "", body=(listing.get("body") or "")[:900],
            price=listing.get("price"),
        )
        try:
            results = self.llm.classify_batch([probe], [])
        except LLMError:
            return None
        result = results.get(listing["external_id"])
        if not result:
            return None
        apply_result(probe, result)
        for key in ("brand", "model", "condition"):
            listing[key] = getattr(probe, key)
        return {
            "brand": probe.brand, "model": probe.model, "item_type": probe.item_type,
            "condition": probe.condition, "scam_risk": probe.scam_risk,
            "wants_to": probe.wants_to,
        }

    def _roster_hint(self, listing: dict) -> str:
        if listing.get("forum") not in self.handgun_forums.get(listing.get("source", ""), set()):
            return "n/a (long gun — roster does not apply)"
        if self.roster is None or not listing.get("brand") or not listing.get("model"):
            return "unknown (no local roster data)"
        return self.roster.lookup(listing["brand"], listing["model"])

    def _ensure_body(self, listing: dict, log=_log) -> tuple[str, str | None] | None:
        """Valuation context wants the FULL ad text; snippets are thin.
        Fetch lazily so only in-radius queue rows cost a request. Returns
        (body, image_url) for the main thread to persist (None if unchanged)."""
        adapter = self.adapters.get(listing.get("source", ""))
        if adapter is None or not getattr(adapter, "thread_body", None):
            return None
        body = listing.get("body") or ""
        if body and len(body) >= 500:
            return None
        try:
            full, image_url = adapter.thread_body(listing["url"])
        except Exception as exc:  # noqa: BLE001 — thin context beats no context
            log(f"{listing['source']}: body fetch failed for {listing['external_id']}: {exc}")
            return None
        listing["body"] = full
        listing["image_url"] = image_url or listing.get("image_url")
        return full, image_url

    def _comps_block(self, listing: dict) -> str:
        if not listing.get("brand") or not listing.get("model"):
            return "none yet"
        comps = self.db.comps(listing["brand"], listing["model"],
                              (listing["source"], listing["external_id"]), days=540, limit=8)
        if not comps:
            return "none yet"
        lines = []
        for c in comps:
            when = (c.get("posted_at") or "")[:10]
            lines.append(f"- ${c['price_usd']:.0f} · {when} · {(c.get('title') or '')[:70]} [{c.get('status') or '?'}]")
        return "\n".join(lines)

    def _user_prompt(self, listing: dict) -> str:
        parts = [
            f"TITLE: {listing['title']}",
            f"ASKING: ${listing.get('price_usd')}",
        ]
        if listing.get("condition"):
            parts.append(f"CONDITION (classified): {listing['condition']}")
        loc = listing.get("city") or listing.get("location_raw") or "unknown"
        parts.append(f"LOCATION: {loc} ({listing.get('distance_miles'):.0f} mi away, geo={listing.get('geo_quality')})")
        parts.append(f"ROSTER LOOKUP (deterministic): {self._roster_hint(listing)}")
        parts.append(f"LOCAL COMPS (our own DB, same make+model):\n{listing.get('comps_block') or self._comps_block(listing)}")
        parts.append(f"LISTING BODY:\n{(listing.get('body') or '(no body fetched)')[:4000]}")
        return "\n\n".join(parts)

    # --- execution ---

    def _executor(self, name: str, arguments: dict) -> str:
        if name != "web_search" or self.search is None:
            return f"tool {name!r} unavailable"
        query = arguments.get("query") or arguments.get("search_query") or ""
        if not query:
            return "empty query"
        return self.search.search(query)

    def _alert_listing(self, listing: dict, v: dict) -> Listing:
        dist = listing.get("distance_miles")
        loc = f"{listing.get('city') or 'unknown'}" + (f" · {dist:.0f} mi" if dist is not None else "")
        att = v.get("attachments") or []
        att_lines = "\n".join(f"  • {a.get('item')} (+${_f(a.get('est_value_usd'), 0):.0f})" for a in att[:6])
        mid = _f(v.get("market_mid"))
        per_item = bool(v.get("asking_price_is_per_item"))
        # per-item listings are scored against the TOTAL ask, never the unit price
        asking = (_f(v.get("asking_total")) if per_item else None) or _f(v.get("asking_price")) or listing.get("price_usd")
        unit_note = f" (${_f(v.get('asking_price'), 0):.0f} each)" if per_item else ""
        delta = f"{(asking / mid - 1) * 100:+.0f}% vs market mid" if mid and asking else ""
        roster = v.get("ca_roster")
        roster_note = f"\n🇨🇦 **OFF-ROSTER** (est. premium {_f(v.get('off_roster_premium_pct'), 0):.0f}%)" if roster == "off" else ""
        body = (
            f"{v.get('summary') or ''}\n"
            f"**Ask ${asking:.0f}{unit_note}** {delta} · est value "
            f"${_f(v.get('market_low'), 0):.0f}–${_f(v.get('market_high'), 0):.0f}{roster_note}\n"
            + (f"**Included:**\n{att_lines}\n" if att_lines else "")
            + (f"⚠️ {v['caveats']}" if v.get("caveats") else "")
        )
        return Listing(
            source=listing["source"],
            external_id=listing["external_id"],
            url=listing["url"],
            title=f"🔥 DEAL {v.get('deal_score')}/100 — {(listing.get('title') or '')[:180]}",
            price=f"${asking:.0f}{unit_note}" if asking else listing.get("price"),
            body=body[:3900],
            location=loc,
            author=listing.get("author"),
            matched_keywords=[f"deal:{v.get('verdict')}"],
            scam_risk=v.get("scam_risk"),
            image_url=listing.get("image_url"),
        )

    def _should_alert(self, v: dict, previous: dict | None) -> bool:
        try:
            score = int(v.get("deal_score"))
        except (TypeError, ValueError):
            return False
        if score < self.cfg.alert_min_score or v.get("verdict") not in ("great", "good"):
            return False
        if v.get("scam_risk") == "high":
            return False
        if previous is None or not previous.get("alerted_at"):
            return True
        # already alerted once — only a real price drop earns another ping
        prev_ask, ask = _f(previous.get("asking_price")), _f(v.get("asking_price"))
        return bool(prev_ask and ask and ask < prev_ask * 0.95)

    def value_one(self, listing: dict, log=_log) -> dict:
        """Full valuation for one queue row (single-threaded path)."""
        previous = self.db.latest_valuation(listing["source"], listing["external_id"])
        outcome = self._work_one(listing, log=log)
        return self._finish_one(listing, previous, outcome, {"queued": 1, "valued": 0, "alerted": 0, "errors": 0}, log)

    # --- worker: pure LLM/network work, NO DB access (keeps SQLite
    # single-threaded when valuations run concurrently) ---

    def _work_one(self, listing: dict, log=_log) -> dict:
        """Returns {'identity': ..., 'body': ..., 'valuation': v} or {'error': ...}."""
        try:
            identity = self._ensure_identity(listing)
            fetched_body = self._ensure_body(listing, log=log)
            tools = [WEB_SEARCH_TOOL] if self.search else []
            user = self._user_prompt(listing)
            reply = self.llm.run_tool_loop(
                VALUATION_SYSTEM, user, tools, self._executor,
                model=self.cfg.model, thinking="on" if self.cfg.thinking else "disabled",
            )
            v = extract_json(reply)
            # the queue row's price is the ask we're judging — the model's echo
            # of it can lag (e.g. after a price-edit re-queue), so overwrite
            v["asking_price"] = listing.get("price_usd")
            for key in ("market_low", "market_mid", "market_high", "off_roster_premium_pct",
                        "asking_price", "asking_total"):
                v[key] = _f(v.get(key))
            v["asking_price_is_per_item"] = bool(v.get("asking_price_is_per_item"))
            if v["asking_price_is_per_item"] and not v.get("asking_total"):
                # per-item flag without a total is a mis-parse — don't let it
                # masquerade as a screaming deal
                v["asking_total"] = v.get("asking_price")
            try:
                v["deal_score"] = max(0, min(100, int(v.get("deal_score"))))
            except (TypeError, ValueError):
                v["deal_score"] = None
            if v.get("verdict") not in ("great", "good", "fair", "poor", "overpriced", "unclear"):
                v["verdict"] = "unclear"
            return {"identity": identity, "body": fetched_body, "valuation": v}
        except LLMError as exc:
            return {"error": str(exc)[:400]}

    def _finish_one(self, listing: dict, previous: dict | None, outcome: dict, stats: dict, log=_log) -> dict:
        """Main-thread persistence + alerting for one completed worker."""
        if outcome.get("identity"):
            self.db.set_llm_fields(listing["source"], listing["external_id"], outcome["identity"])
        if outcome.get("body"):
            body_text, image_url = outcome["body"]
            self.db.set_body(listing["source"], listing["external_id"], body_text, image_url)
        v = outcome.get("valuation")
        if v is None:
            stats["errors"] += 1
            self.db.insert_valuation(
                listing["source"], listing["external_id"], {"error": outcome.get("error")}, self.cfg.model
            )
            log(f"valuation failed for {listing['external_id']}: {str(outcome.get('error'))[:140]}")
            return stats
        vid = self.db.insert_valuation(listing["source"], listing["external_id"], v, self.cfg.model)
        stats["valued"] += 1
        if self._should_alert(v, previous) and self.alerters:
            errors = self.alerters.send_all([self._alert_listing(listing, v)])
            for err in errors:
                log(f"! deal alert error: {err}")
            if not errors:
                self.db.mark_valuation_alerted(vid)
                stats["alerted"] += 1
                log(
                    f"🔥 deal alert: {listing['title'][:60]!r} "
                    f"score={v['deal_score']} ask=${_f(v.get('asking_price'), 0):.0f}"
                )
        if v.get("deal_score") is not None:
            log(
                f"valued {listing['external_id']}: {v.get('verdict')} "
                f"({v['deal_score']}/100) ask=${_f(v.get('asking_price'), 0):.0f} "
                f"mid=${_f(v.get('market_mid'), 0):.0f} — {(listing.get('title') or '')[:48]}"
            )
        return stats

    def run(self, limit: int | None = None, log=_log) -> dict:
        """Drain the queue: workers in a paced pool, DB writes on this thread.

        Each valuation takes ~2 min of model+web time, so hitting
        rate_per_minute requires concurrency — launches are spaced at
        60/rate seconds, up to max_concurrent in flight.
        """
        cap = limit or self.cfg.max_per_cycle
        queue = self.db.valuation_queue(
            window_days=self.cfg.eval_window_days,
            radius_miles=self.radius,
            limit=cap,
            gun_forums=self.cfg.gun_forums,
        )
        stats = {"queued": len(queue), "valued": 0, "alerted": 0, "errors": 0}
        if not queue:
            return stats
        pace = (60.0 / self.cfg.rate_per_minute) if getattr(self.cfg, "rate_per_minute", 0) else 0.0
        workers = max(1, min(getattr(self.cfg, "max_concurrent", 1) or 1, len(queue)))
        # all DB reads up front, on this thread — workers stay pure
        previous = {}
        for l in queue:
            previous[l["external_id"]] = self.db.latest_valuation(l["source"], l["external_id"])
            l["comps_block"] = self._comps_block(l)
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=workers) as ex:
            futures = []
            for i, listing in enumerate(queue):
                futures.append(ex.submit(self._work_one, listing, log))
                if pace and i < len(queue) - 1:
                    time.sleep(pace)
            for listing, fut in zip(queue, futures):
                try:
                    outcome = fut.result()
                except Exception as exc:  # noqa: BLE001 — worker exploded; record and continue
                    outcome = {"error": f"{type(exc).__name__}: {exc}"[:400]}
                stats = self._finish_one(listing, previous[listing["external_id"]], outcome, stats, log)
        return stats
