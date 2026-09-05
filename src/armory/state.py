"""Minimal JSON state: last-check times, recent-ID rings, keywords, rules.

No database — everything armory needs to know "what's new" and "what to watch
for" lives in one small file (armory.state.json).
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# IDs remembered per source for exact dedupe; beyond this, the last_check
# timestamp takes over. Sized to cover a full day of busy-site feed churn.
RING_SIZE = 500

# Listings that have already alerted (or were suppressed as overflow) —
# bumped/re-detected listings never re-alert. Larger than the ring because
# sellers bump for weeks.
ALERTED_SIZE = 5000

DEFAULT_STATE: dict = {"sources": {}, "keywords": [], "rules": [], "alerted": {}}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


@dataclass
class SourceState:
    last_check: str | None = None
    recent_ids: list[str] = field(default_factory=list)


class StateConflictError(RuntimeError):
    """Another writer changed the same section; reload before retrying."""


class StateStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._read()
        self._saved = deepcopy(self.data)

    def _read(self) -> dict:
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        for key, value in DEFAULT_STATE.items():
            data.setdefault(key, deepcopy(value))
        return data

    # --- sources ---

    def source(self, name: str) -> SourceState:
        raw = self.data["sources"].get(name) or {}
        return SourceState(last_check=raw.get("last_check"), recent_ids=list(raw.get("recent_ids", [])))

    def is_first_run(self, name: str) -> bool:
        s = self.source(name)
        return s.last_check is None and not s.recent_ids

    def update_source(self, name: str, ids: list[str], last_check: str) -> None:
        ring = ids[:RING_SIZE]
        self.data["sources"][name] = {"last_check": last_check, "recent_ids": ring}

    # --- alerted cache (one alert per listing, ever) ---

    def has_alerted(self, source: str, external_id: str) -> bool:
        return external_id in set(self.data["alerted"].get(source, []))

    def mark_alerted(self, source: str, external_ids: list[str]) -> None:
        seen = set(self.data["alerted"].get(source, []))
        for external_id in external_ids:
            seen.add(external_id)
        # newest-first FIFO trim keeps the file bounded
        self.data["alerted"][source] = sorted(seen)[-ALERTED_SIZE:]

    # --- keywords ---

    def keywords(self) -> list[dict]:
        return self.data["keywords"]

    def add_keyword(self, raw_term: str, is_regex: bool = False, mode: str | None = None) -> bool:
        """Add a keyword. Mode comes from the argument or the term's prefix
        (-term / +term / {trade}term); explicit mode wins."""
        from .matching import parse_term

        term, parsed_mode = parse_term(raw_term)
        mode = mode or parsed_mode
        if not term:
            return False
        if any(k["term"] == term and k.get("mode", "include") == mode for k in self.data["keywords"]):
            return False
        # a plain include and the same term with a trade flag can coexist only
        # as one entry — trade is a superset, so it replaces plain
        self.data["keywords"] = [
            k for k in self.data["keywords"] if not (k["term"] == term and mode == "trade")
        ]
        self.data["keywords"].append({"term": term, "is_regex": bool(is_regex), "mode": mode})
        return True

    def remove_keyword(self, raw_term: str) -> bool:
        from .matching import parse_term

        term, _ = parse_term(raw_term)
        before = len(self.data["keywords"])
        self.data["keywords"] = [k for k in self.data["keywords"] if k["term"] != term]
        return len(self.data["keywords"]) < before

    def seed_keywords(self, terms: list[str]) -> int:
        if self.data["keywords"] or not terms:
            return 0
        for t in terms:
            self.add_keyword(t)
        return len(terms)

    # --- rules ---

    def rules(self) -> list[str]:
        return self.data["rules"]

    def add_rule(self, rule: str) -> bool:
        if rule in self.data["rules"]:
            return False
        self.data["rules"].append(rule)
        return True

    def remove_rule(self, rule: str) -> bool:
        if rule in self.data["rules"]:
            self.data["rules"].remove(rule)
            return True
        return False

    # --- persistence ---

    def save(self) -> None:
        # Lock a separate, stable inode: the state file itself gets replaced.
        with self.path.with_name(self.path.name + ".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            current = self._read()
            for key in self.data.keys() | self._saved.keys():
                if self.data.get(key) == self._saved.get(key):
                    continue
                if current.get(key) != self._saved.get(key) and current.get(key) != self.data.get(key):
                    raise StateConflictError(f"State section {key!r} changed on disk; reload and retry")
                if key in self.data:
                    current[key] = self.data[key]
                else:
                    current.pop(key, None)
            tmp = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w", dir=self.path.parent, prefix=self.path.name + ".",
                    suffix=".tmp", delete=False,
                ) as stream:
                    tmp = Path(stream.name)
                    json.dump(current, stream, indent=2)
                    stream.flush()
                    os.fsync(stream.fileno())
                tmp.replace(self.path)
            finally:
                if tmp is not None:
                    tmp.unlink(missing_ok=True)
            self.data = current
            self._saved = deepcopy(current)
