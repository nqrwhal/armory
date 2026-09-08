"""CA DOJ handgun roster: fetch, normalize, and look up roster status.

Sources (both server-rendered HTML tables, no login):
  https://oag.ca.gov/firearms/certified-handguns/search   — currently certified
  https://oag.ca.gov/firearms/de-certified-handguns       — removed models

Matching is intentionally fuzzy: the roster writes "19 / Steel, Polymer" where
a listing says "Glock 19 Gen 3". Exact normalized hits win; then prefix
matches, certified answers winning ties. Long guns never consult the roster
(handgun-only law) — the caller passes n/a without asking.
"""

from __future__ import annotations

import re
import time

from bs4 import BeautifulSoup

CERTIFIED_URL = "https://oag.ca.gov/firearms/certified-handguns/search"
DECERTIFIED_URL = "https://oag.ca.gov/firearms/de-certified-handguns"

# short-form brands an LLM or seller might extract vs the DOJ's spelling
MAKE_ALIASES: dict[str, str] = {
    "sw": "smithwesson",
    "smithandwesson": "smithwesson",
    "sig": "sigsauer",
    "sigsaue": "sigsauer",
    "springfield": "springfieldarmory",
    "fn": "fnamerica",
    "fnh": "fnamerica",
    "sturm": "sturmruer",
    "ruger": "sturmruer",
    "kimberusa": "kimber",
    "hesse": "hessearms",
}

_STRIP = re.compile(r"[^a-z0-9]+")
_LEADING_LETTERS = re.compile(r"^[a-z]+(?=\d)")


def normalize_make(make: str) -> str:
    n = _STRIP.sub("", (make or "").lower())
    return MAKE_ALIASES.get(n, n)


def normalize_model(model: str, make: str = "") -> str:
    n = _STRIP.sub("", (model or "").lower())
    if normalize_make(make) == "glock" and re.fullmatch(r"g\d+[a-z]*", n):
        n = n[1:]  # "g19x" → "19x": the roster lists Glock models bare
    return n


def _loose(model_n: str) -> str:
    """Drop a leading letter run before digits: the roster writes '365-9…'
    and '19 / Steel' where listings say 'P365' or 'G19'."""
    return _LEADING_LETTERS.sub("", model_n) or model_n


def _split_model(model: str) -> list[str]:
    """Roster models carry a material suffix after '/' — index both forms."""
    out = [model.strip()]
    if "/" in model:
        out.append(model.split("/", 1)[0].strip())
    return out


def _parse_table(html: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[tuple[str, str]] = []
    for tr in soup.select("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"], recursive=False)]
        if len(cells) >= 2 and cells[0] and cells[0].lower() != "manufacturer":
            out.append((cells[0], cells[1]))
    return out


def fetch_roster(fetcher) -> list[tuple[str, str, str, str, str]]:
    """Returns roster rows: (make_norm, model_norm, status, raw_make, raw_model)."""
    entries: list[tuple[str, str, str, str, str]] = []

    def add(html: str, status: str) -> None:
        for raw_make, raw_model in _parse_table(html):
            make_n = normalize_make(raw_make)
            if not make_n:
                continue
            for model_raw in _split_model(raw_model):
                model_n = normalize_model(model_raw, raw_make)
                if model_n:
                    entries.append((make_n, model_n, status, raw_make, raw_model))

    resp = fetcher.get(CERTIFIED_URL)
    if resp.status_code != 200:
        raise RuntimeError(f"certified roster page returned HTTP {resp.status_code}")
    add(resp.text, "on")
    time.sleep(1.0)
    resp = fetcher.get(DECERTIFIED_URL)
    if resp.status_code != 200:
        raise RuntimeError(f"de-certified roster page returned HTTP {resp.status_code}")
    add(resp.text, "off")
    return entries


class RosterIndex:
    """In-memory fuzzy index over the roster table (a few thousand rows)."""

    _GEN = re.compile(r"gen\d")

    def __init__(self, db):
        self._on: dict[str, set[str]] = {}
        self._off: dict[str, set[str]] = {}
        for row in db.conn.execute("SELECT make, model, status FROM roster"):
            bucket = self._on if row["status"] == "on" else self._off
            bucket.setdefault(row["make"], set()).add(row["model"])

    @classmethod
    def _gens(cls, model_n: str) -> set[str]:
        return set(cls._GEN.findall(model_n))

    def lookup(self, make: str, model: str) -> str:
        """'on' | 'off' | 'unknown'."""
        make_n = normalize_make(make)
        model_n = normalize_model(model, make)
        if not make_n or not model_n:
            return "unknown"
        on_models, off_models = self._on.get(make_n, set()), self._off.get(make_n, set())
        if model_n in on_models:
            return "on"
        if model_n in off_models:
            return "off"
        loose = _loose(model_n)
        if loose != model_n:
            if loose in on_models:
                return "on"
            if loose in off_models:
                return "off"
        # prefix matches, longest first; certified wins ties (a re-listed
        # model is on the roster today regardless of its removal history)
        best: tuple[tuple[int, int], str] | None = None
        best_cand = ""
        for status, models in (("on", on_models), ("off", off_models)):
            rank = 0 if status == "on" else 1
            for m in models:
                m_loose = _loose(m)
                for q, cand in ((model_n, m), (loose, m_loose)):
                    if q and cand and (q.startswith(cand) or cand.startswith(q)):
                        key = (-len(cand), rank)
                        if best is None or key < best[0]:
                            best = (key, status)
                            best_cand = cand
        if best is None:
            return "unknown"
        # generation guard: "17gen4" prefix-matching a bare "17" would claim
        # a Gen 4 is on the roster when only the Gen 3 is — the DOJ roster
        # is overwhelmingly Gen 3-era for Glock. Downgrade to unknown and let
        # the LLM adjudicate against the live roster.
        query_gens = self._gens(model_n)
        cand_gens = self._gens(best_cand)
        if not cand_gens and make_n == "glock" and best_cand.isdigit():
            cand_gens = {"gen3"}  # a bare Glock number on the roster IS the Gen 3
        if query_gens and not (query_gens & cand_gens):
            return "unknown"
        return best[1]

    def counts(self) -> dict:
        return {
            "on": sum(len(v) for v in self._on.values()),
            "off": sum(len(v) for v in self._off.values()),
        }
