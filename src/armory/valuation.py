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

Return STRICT JSON only:
{"make": str|null, "model": str|null, "variant": str|null,
 "confidence": "high|medium|low",
 "attachments": [{"item": str, "est_value_usd": number}],
 "market_low": number, "market_mid": number, "market_high": number,
 "value_basis": "1-2 sentences: what the range is built on",
 "ca_roster": "on|off|unknown|n/a",
 "off_roster_premium_pct": number|null,
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
    ):
        self.db = db
        self.llm = llm
        self.cfg = cfg
        self.radius = radius_miles
        self.search = search if (search and search.configured and cfg.web_search) else None
        self.roster = roster
        self.alerters = alerters

    # --- context building ---

    def _ensure_identity(self, listing: dict) -> None:
        """Backfilled rows never met the poller's classify pass — fill make/model."""
        if listing.get("brand") and listing.get("model"):
            return
        probe = Listing(
            source=listing["source"], external_id=listing["external_id"], url=listing["url"],
            title=listing["title"] or "", body=(listing.get("body") or "")[:900],
            price=listing.get("price"),
        )
        try:
            results = self.llm.classify_batch([probe], [])
        except LLMError:
            return
        result = results.get(listing["external_id"])
        if result:
            apply_result(probe, result)
            self.db.set_llm_fields(listing["source"], listing["external_id"], {
                "brand": probe.brand, "model": probe.model, "item_type": probe.item_type,
                "condition": probe.condition, "scam_risk": probe.scam_risk,
                "wants_to": probe.wants_to,
            })
            for key in ("brand", "model", "condition"):
                listing[key] = getattr(probe, key)

    def _roster_hint(self, listing: dict) -> str:
        if listing.get("forum") != "handguns":
            return "n/a (long gun — roster does not apply)"
        if self.roster is None or not listing.get("brand") or not listing.get("model"):
            return "unknown (no local roster data)"
        return self.roster.lookup(listing["brand"], listing["model"])

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
        parts.append(f"LOCAL COMPS (our own DB, same make+model):\n{self._comps_block(listing)}")
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
        asking = _f(v.get("asking_price")) or listing.get("price_usd")
        delta = f"{(asking / mid - 1) * 100:+.0f}% vs market mid" if mid and asking else ""
        roster = v.get("ca_roster")
        roster_note = f"\n🇨🇦 **OFF-ROSTER** (est. premium {_f(v.get('off_roster_premium_pct'), 0):.0f}%)" if roster == "off" else ""
        body = (
            f"{v.get('summary') or ''}\n"
            f"**Ask ${asking:.0f}** {delta} · est value "
            f"${_f(v.get('market_low'), 0):.0f}–${_f(v.get('market_high'), 0):.0f}{roster_note}\n"
            + (f"**Included:**\n{att_lines}\n" if att_lines else "")
            + (f"⚠️ {v['caveats']}" if v.get("caveats") else "")
        )
        return Listing(
            source=listing["source"],
            external_id=listing["external_id"],
            url=listing["url"],
            title=f"🔥 DEAL {v.get('deal_score')}/100 — {(listing.get('title') or '')[:180]}",
            price=f"${asking:.0f}" if asking else listing.get("price"),
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
        """Full valuation for one queue row. Returns the stored result dict."""
        self._ensure_identity(listing)
        previous = self.db.latest_valuation(listing["source"], listing["external_id"])
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
        # sanitize numeric fields
        for key in ("market_low", "market_mid", "market_high", "off_roster_premium_pct", "asking_price"):
            v[key] = _f(v.get(key))
        try:
            v["deal_score"] = max(0, min(100, int(v.get("deal_score"))))
        except (TypeError, ValueError):
            v["deal_score"] = None
        if v.get("verdict") not in ("great", "good", "fair", "poor", "overpriced", "unclear"):
            v["verdict"] = "unclear"
        vid = self.db.insert_valuation(listing["source"], listing["external_id"], v, self.cfg.model)

        if self._should_alert(v, previous) and self.alerters:
            errors = self.alerters.send_all([self._alert_listing(listing, v)])
            for err in errors:
                log(f"! deal alert error: {err}")
            if not errors:
                self.db.mark_valuation_alerted(vid)
                log(
                    f"🔥 deal alert: {listing['title'][:60]!r} "
                    f"score={v['deal_score']} ask=${_f(v.get('asking_price'), 0):.0f}"
                )
        return v

    def run(self, limit: int | None = None, log=_log) -> dict:
        cap = limit or self.cfg.max_per_cycle
        queue = self.db.valuation_queue(
            window_days=self.cfg.eval_window_days,
            radius_miles=self.radius,
            limit=cap,
        )
        stats = {"queued": len(queue), "valued": 0, "alerted": 0, "errors": 0}
        for listing in queue:
            try:
                v = self.value_one(listing, log=log)
                stats["valued"] += 1
                if v.get("deal_score") is not None:
                    log(
                        f"valued {listing['external_id']}: {v.get('verdict')} "
                        f"({v['deal_score']}/100) ask=${_f(v.get('asking_price'), 0):.0f} "
                        f"mid=${_f(v.get('market_mid'), 0):.0f} — {(listing.get('title') or '')[:48]}"
                    )
            except LLMError as exc:
                stats["errors"] += 1
                self.db.insert_valuation(
                    listing["source"], listing["external_id"], {"error": str(exc)[:400]}, self.cfg.model
                )
                log(f"valuation failed for {listing['external_id']}: {str(exc)[:140]}")
        return stats
