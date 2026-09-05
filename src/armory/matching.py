"""Keyword matching with modes and listing-intent awareness.

Term syntax (state file / config seeds / `armory keywords add`):
  term          include — match listings that HAVE/want-to-sell the item
  +term         explicit include (same as plain)
  -term         exclude — suppresses the listing even when includes match
  {trade}term   include AND match demand-side (WTT/WTB) listings too

Demand-side listings (someone seeking or trading FOR the item) never alert on
plain includes: a "WTB Glock 19" post is noise when you're watching for Glocks
to buy. Intent comes from source-native data or the LLM when available, with a
title-convention heuristic as the floor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Listing
from .state import StateStore

# Adjective-heavy listings make plain substring matching noisy ("glock" in
# "glockbox"), so plain terms get boundary assertions. \b alone misbehaves at
# digits/symbols, hence the lookaround pair.
def compile_term(term: str, is_regex: bool = False) -> re.Pattern[str]:
    if is_regex:
        return re.compile(term, re.IGNORECASE)
    escaped = re.escape(term.strip())
    return re.compile(rf"(?<!\w){escaped}(?!\w)", re.IGNORECASE)


# --- term syntax ---

def parse_term(raw: str) -> tuple[str, str]:
    """Split a raw term into (term, mode). Modes: include | exclude | trade."""
    t = raw.strip()
    if t.startswith("{trade}"):
        return t[len("{trade}") :].strip(), "trade"
    if t.startswith("-"):
        return t[1:].strip(), "exclude"
    if t.startswith("+"):
        return t[1:].strip(), "include"
    return t, "include"


# --- listing intent ---

_WTT = re.compile(r"\b(wtt|want(?:ing)? to trade)\b", re.IGNORECASE)
_WTB = re.compile(r"\b(wtb|want(?:ing)? to buy|iso|in search of)\b", re.IGNORECASE)


def detect_wants_to(listing: Listing) -> str | None:
    if listing.wants_to in ("wts", "wtt", "wtb"):
        return listing.wants_to
    title = listing.title or ""
    if _WTT.search(title):
        return "wtt"
    if _WTB.search(title):
        return "wtb"
    return None


# --- matcher compilation and evaluation ---

@dataclass
class Matchers:
    include: list[tuple[str, re.Pattern[str], bool]] = field(default_factory=list)  # (term, pat, trade_ok)
    exclude: list[tuple[str, re.Pattern[str]]] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.include or self.exclude)


def load_matchers(state: StateStore) -> Matchers:
    out = Matchers()
    for k in state.keywords():
        term, mode = k["term"], k.get("mode") or parse_term(k["term"])[1]
        # entries store the normalized term + explicit mode; older entries
        # (no mode) keep whatever prefix parsing says, defaulting to include
        pat = compile_term(term, k.get("is_regex", False))
        if mode == "exclude":
            out.exclude.append((term, pat))
        else:
            out.include.append((term, pat, mode == "trade"))
    return out


def match_listing(listing: Listing, matchers: Matchers) -> list[str]:
    """Terms that should alert for this listing ([] = no alert)."""
    if not matchers:
        return []
    text = listing.search_text()
    if any(pat.search(text) for _, pat in matchers.exclude):
        return []
    intent = detect_wants_to(listing)
    demand_side = intent in ("wtt", "wtb")
    return [
        term
        for term, pat, trade_ok in matchers.include
        if pat.search(text) and (not demand_side or trade_ok)
    ]
