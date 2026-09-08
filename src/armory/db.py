"""SQLite persistence: every listing ever seen, valuations, roster, progress.

Deliberately stdlib-only (sqlite3). WAL + busy_timeout so the watch loop and
CLI commands can write concurrently. Bodies are capped upstream (~8 KB), so
even years of marketplace threads stay in the tens of MB.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .models import ThreadRow

SCHEMA = """
CREATE TABLE IF NOT EXISTS listings (
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  url TEXT,
  title TEXT,
  body TEXT,
  price TEXT,
  price_usd REAL,
  category TEXT,
  forum TEXT,
  location_raw TEXT,
  city TEXT, state TEXT, zip TEXT,
  lat REAL, lon REAL, distance_miles REAL, geo_quality TEXT,
  author TEXT,
  posted_at TEXT,
  last_post_at TEXT,
  replies INTEGER DEFAULT 0,
  views INTEGER DEFAULT 0,
  wants_to TEXT,
  status TEXT DEFAULT 'unknown',        -- open | sold | unknown
  brand TEXT, model TEXT, item_type TEXT, condition TEXT,
  scam_risk TEXT,
  image_url TEXT,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL,
  needs_enrich INTEGER DEFAULT 0,
  PRIMARY KEY (source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_listings_forum ON listings(forum);
CREATE INDEX IF NOT EXISTS idx_listings_posted ON listings(posted_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_lastpost ON listings(last_post_at DESC);
CREATE INDEX IF NOT EXISTS idx_listings_model ON listings(brand, model);

CREATE TABLE IF NOT EXISTS valuations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  asking_price REAL,
  market_low REAL, market_mid REAL, market_high REAL,
  attachments_json TEXT,
  ca_roster TEXT,                        -- on | off | unknown | n/a
  off_roster_premium_pct REAL,
  deal_score INTEGER,
  verdict TEXT,                          -- great | good | fair | poor | overpriced | unclear
  summary TEXT,
  make TEXT, model_name TEXT, variant TEXT, confidence TEXT,
  sources_json TEXT,
  model_used TEXT,
  error TEXT,
  alerted_at TEXT,
  FOREIGN KEY (source, external_id) REFERENCES listings(source, external_id)
);
CREATE INDEX IF NOT EXISTS idx_valuations_listing ON valuations(source, external_id);

CREATE TABLE IF NOT EXISTS roster (
  make TEXT NOT NULL,
  model TEXT NOT NULL,
  status TEXT NOT NULL,                  -- on | off
  raw_make TEXT, raw_model TEXT,
  updated_at TEXT,
  PRIMARY KEY (make, model)
);

CREATE TABLE IF NOT EXISTS comps_archive (
  source TEXT NOT NULL,
  external_id TEXT NOT NULL,
  forum TEXT,
  brand TEXT, model TEXT,
  price_usd REAL,
  posted_at TEXT,
  archived_at TEXT NOT NULL,
  PRIMARY KEY (source, external_id)
);

CREATE TABLE IF NOT EXISTS backfill_progress (
  forum TEXT NOT NULL PRIMARY KEY,
  pages_done INTEGER DEFAULT 0,
  done INTEGER DEFAULT 0,
  updated_at TEXT
);
"""

# Freshness for valuation uses the LATER of thread start and last activity:
# an old thread bumped today with a price drop is a live listing.
_QUEUE_FRESH = "MAX(COALESCE(posted_at, ''), COALESCE(last_post_at, ''))"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sql_ts(dt: datetime | str | None) -> str | None:
    """Timestamps in SQLite's own format so string compare with datetime('now')
    works ('T'-separated ISO strings sort after every space-separated date)."""
    if dt is None:
        return None
    if isinstance(dt, str):
        dt = datetime.fromisoformat(dt)
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    return dt.strftime("%Y-%m-%d %H:%M:%S")


@dataclass(slots=True)
class UpsertResult:
    new: list[dict]
    changed: list[dict]  # existing rows whose title/author changed (price edits, SOLD)


class Db:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        # isolation_level=None → autocommit; multi-statement batches below
        # manage their own BEGIN/COMMIT explicitly.
        self.conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- listings ---

    def upsert_threads(self, source: str, rows: list[ThreadRow]) -> UpsertResult:
        """Insert new threads / refresh seen ones. Returns new + changed rows.

        A changed title on an existing thread usually means a price edit or a
        SOLD marker — the caller re-runs title parsing for those.
        """
        from .adapters.calguns import parse_title  # title conventions, used as the fallback

        now = utcnow_iso()
        result = UpsertResult(new=[], changed=[])
        self.conn.execute("BEGIN")
        try:
            for row in rows:
                existing = self.conn.execute(
                    "SELECT title, author, price_usd, status, wants_to FROM listings WHERE source=? AND external_id=?",
                    (source, row.external_id),
                ).fetchone()
                if existing is None:
                    parsed = parse_title(row.title)
                    price_usd = row.price_usd if row.price_usd is not None else parsed["price_usd"]
                    price = row.price or (f"${price_usd:g}" if price_usd else None)
                    wants_to = row.wants_to or parsed["wants_to"]
                    sold = row.sold or parsed["sold"]
                    status = "sold" if sold else ("open" if row.last_post_at else "unknown")
                    self.conn.execute(
                        """INSERT INTO listings (source, external_id, url, title, price, price_usd,
                             forum, author, posted_at, last_post_at, replies, views, wants_to, status,
                             location_raw, body, first_seen, last_seen, needs_enrich)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            source, row.external_id, row.url, row.title, price, price_usd,
                            row.forum, row.author,
                            sql_ts(row.posted_at), sql_ts(row.last_post_at),
                            row.replies, row.views, wants_to, status,
                            row.location, row.body or None, now, now,
                            1 if price_usd is None else 0,
                        ),
                    )
                    result.new.append(dict(source=source, external_id=row.external_id, title=row.title, forum=row.forum))
                else:
                    changed = existing["title"] != row.title or (row.author and existing["author"] != row.author)
                    if changed:
                        parsed = parse_title(row.title)
                        price_usd = row.price_usd if row.price_usd is not None else parsed["price_usd"]
                        if row.sold or parsed["sold"]:
                            status = "sold"
                        elif existing["status"] == "sold":
                            status = "sold"  # SOLD edits are not reliably undone; leave it
                        else:
                            status = "open" if row.last_post_at else existing["status"]
                        self.conn.execute(
                            """UPDATE listings SET title=?, author=?, price_usd=COALESCE(?, price_usd),
                                 price=COALESCE(?, price), wants_to=?, status=?, url=?,
                                 posted_at=COALESCE(posted_at, ?)
                               WHERE source=? AND external_id=?""",
                            (
                                row.title, row.author, price_usd,
                                row.price or (f"${price_usd:g}" if price_usd else None),
                                row.wants_to or parsed["wants_to"] or existing["wants_to"], status, row.url,
                                sql_ts(row.posted_at),
                                source, row.external_id,
                            ),
                        )
                        result.changed.append(dict(source=source, external_id=row.external_id, title=row.title, forum=row.forum))
                    self.conn.execute(
                        """UPDATE listings SET last_seen=?, last_post_at=COALESCE(?, last_post_at),
                             replies=MAX(replies, ?), views=MAX(views, ?),
                             location_raw=COALESCE(location_raw, ?)
                           WHERE source=? AND external_id=?""",
                        (
                            now,
                            sql_ts(row.last_post_at),
                            row.replies, row.views, row.location,
                            source, row.external_id,
                        ),
                    )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return result

    def get_listing(self, source: str, external_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM listings WHERE source=? AND external_id=?", (source, external_id)
        ).fetchone()
        return dict(row) if row else None

    def set_body(self, source: str, external_id: str, body: str, image_url: str | None = None) -> None:
        self.conn.execute(
            "UPDATE listings SET body=?, image_url=COALESCE(?, image_url), needs_enrich=0 WHERE source=? AND external_id=?",
            (body, image_url, source, external_id),
        )
        self.conn.commit()

    def set_geo(self, source: str, external_id: str, lat: float, lon: float, distance: float | None,
                quality: str, city: str | None, state: str | None, zip_code: str | None) -> None:
        self.conn.execute(
            """UPDATE listings SET lat=?, lon=?, distance_miles=?, geo_quality=?, city=COALESCE(?, city),
                 state=COALESCE(?, state), zip=COALESCE(?, zip) WHERE source=? AND external_id=?""",
            (lat, lon, distance, quality, city, state, zip_code, source, external_id),
        )
        self.conn.commit()

    def set_llm_fields(self, source: str, external_id: str, fields: dict) -> None:
        cols, vals = [], []
        for col in ("brand", "model", "item_type", "condition", "scam_risk", "wants_to"):
            if fields.get(col):
                cols.append(f"{col}=?")
                vals.append(fields[col])
        if not cols:
            return
        vals.extend([source, external_id])
        self.conn.execute(f"UPDATE listings SET {', '.join(cols)} WHERE source=? AND external_id=?", vals)
        self.conn.commit()

    def set_status(self, source: str, external_id: str, status: str) -> None:
        self.conn.execute(
            "UPDATE listings SET status=? WHERE source=? AND external_id=?", (status, source, external_id)
        )
        self.conn.commit()

    def pending_enrich(self, limit: int = 50, forum: str | None = None) -> list[dict]:
        """Threads whose title hid the price/location — the body must be fetched."""
        q = f"""SELECT source, external_id, url, title, forum, price_usd FROM listings
                 WHERE needs_enrich=1 AND body IS NULL {"AND forum=?" if forum else ""}
                 ORDER BY COALESCE(last_post_at, posted_at, first_seen) DESC LIMIT ?"""
        args = ([forum] if forum else []) + [limit]
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    # --- valuation queue ---

    def valuation_queue(self, window_days: int, radius_miles: float, limit: int = 20,
                        gun_forums: dict[str, list[str]] | None = None) -> list[dict]:
        """In-radius, for-sale, recently-active gun listings needing (re)valuation.

        gun_forums maps source → its gun categories (e.g. calguns→handguns,
        caguns→firearms). A listing re-enters the queue when its asking price
        moved >5% from the latest valuation — a price drop is exactly when a
        fresh opinion matters.
        """
        forums_map = gun_forums or {"calguns": ["handguns", "long_guns"]}
        # distance must filter in SQL: a Python-side filter after LIMIT would
        # starve in-radius rows — the newest-active threads are mostly
        # out-of-radius statewide listings
        pairs, forum_args = [], []
        for src, forums in forums_map.items():
            placeholders = ", ".join("?" * len(forums))
            pairs.append(f"(l.source = ? AND l.forum IN ({placeholders}))")
            forum_args.append(src)
            forum_args.extend(forums)
        forum_where = " OR ".join(pairs)
        q = f"""
        SELECT l.*, v.asking_price AS last_ask, v.id AS last_val_id, v.alerted_at
          FROM listings l
          LEFT JOIN valuations v
            ON v.source = l.source AND v.external_id = l.external_id
           AND v.id = (SELECT MAX(id) FROM valuations v2
                        WHERE v2.source = l.source AND v2.external_id = l.external_id)
         WHERE ({forum_where})
           AND l.wants_to = 'wts'
           AND l.status = 'open'
           AND l.price_usd IS NOT NULL
           AND l.distance_miles IS NOT NULL AND l.distance_miles <= ?
           AND {_QUEUE_FRESH} >= datetime('now', ?)
           AND (
             v.id IS NULL
             OR (ABS(COALESCE(v.asking_price, 0) - l.price_usd) / l.price_usd > 0.05
                 AND (v.error IS NULL OR v.created_at < datetime('now', '-7 days')))
           )
         ORDER BY {_QUEUE_FRESH} DESC
         LIMIT ?"""
        args = [*forum_args, radius_miles, f"-{window_days} days", limit]
        return [dict(r) for r in self.conn.execute(q, args).fetchall()]

    def insert_valuation(self, source: str, external_id: str, result: dict, model_used: str) -> int:
        cur = self.conn.execute(
            """INSERT INTO valuations (source, external_id, created_at, asking_price,
                 market_low, market_mid, market_high, attachments_json, ca_roster,
                 off_roster_premium_pct, deal_score, verdict, summary, make, model_name,
                 variant, confidence, sources_json, model_used, error)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                source, external_id, utcnow_iso(), result.get("asking_price"),
                result.get("market_low"), result.get("market_mid"), result.get("market_high"),
                json.dumps(result.get("attachments")) if result.get("attachments") else None,
                result.get("ca_roster"), result.get("off_roster_premium_pct"),
                result.get("deal_score"), result.get("verdict"), result.get("summary"),
                result.get("make"), result.get("model"), result.get("variant"),
                result.get("confidence"),
                json.dumps(result.get("sources")) if result.get("sources") else None,
                model_used, result.get("error"),
            ),
        )
        self.conn.commit()
        return cur.lastrowid

    def mark_valuation_alerted(self, valuation_id: int) -> None:
        self.conn.execute("UPDATE valuations SET alerted_at=? WHERE id=?", (utcnow_iso(), valuation_id))
        self.conn.commit()

    def latest_valuation(self, source: str, external_id: str) -> dict | None:
        row = self.conn.execute(
            "SELECT * FROM valuations WHERE source=? AND external_id=? ORDER BY id DESC LIMIT 1",
            (source, external_id),
        ).fetchone()
        return dict(row) if row else None

    def comps(self, brand: str, model: str, exclude: tuple[str, str], days: int = 365, limit: int = 8) -> list[dict]:
        """Historical asking prices for the same brand+model — local comps."""
        rows = self.conn.execute(
            f"""SELECT l.source, l.external_id, l.title, l.price_usd, l.posted_at, l.forum, l.status
                  FROM listings l
                 WHERE l.brand=? AND l.model=? AND l.price_usd IS NOT NULL
                   AND NOT (l.source=? AND l.external_id=?)
                   AND COALESCE(l.posted_at, '') >= datetime('now', ?)
                 ORDER BY l.posted_at DESC LIMIT ?""",
            (brand, model, exclude[0], exclude[1], f"-{days} days", limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def top_deals(self, limit: int = 15) -> list[dict]:
        rows = self.conn.execute(
            """SELECT v.*, l.title, l.url, l.distance_miles, l.city, l.status AS listing_status
                 FROM valuations v JOIN listings l
                   ON l.source = v.source AND l.external_id = v.external_id
                WHERE v.deal_score IS NOT NULL AND v.error IS NULL
                  AND v.id = (SELECT MAX(id) FROM valuations v2
                               WHERE v2.source = v.source AND v2.external_id = v.external_id)
                  AND l.status = 'open'
                ORDER BY v.deal_score DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # --- roster ---

    def roster_load(self, entries: list[tuple[str, str, str, str, str]]) -> int:
        """Replace-all load: (make, model, status, raw_make, raw_model)."""
        now = utcnow_iso()
        self.conn.execute("BEGIN")
        try:
            self.conn.execute("DELETE FROM roster")
            self.conn.executemany(
                "INSERT OR REPLACE INTO roster (make, model, status, raw_make, raw_model, updated_at) VALUES (?,?,?,?,?,?)",
                [(mk, md, st, rmk, rmd, now) for mk, md, st, rmk, rmd in entries],
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return len(entries)

    def roster_lookup(self, make: str, model: str) -> str | None:
        """'on' | 'off' | None when the make/model isn't in the table at all."""
        row = self.conn.execute(
            "SELECT status FROM roster WHERE make=? AND model=?", (make, model)
        ).fetchone()
        return row["status"] if row else None

    def roster_count(self) -> int:
        return self.conn.execute("SELECT COUNT(*) AS n FROM roster").fetchone()["n"]

    # --- backfill progress ---

    def backfill_progress(self, forum: str) -> dict:
        row = self.conn.execute("SELECT * FROM backfill_progress WHERE forum=?", (forum,)).fetchone()
        return dict(row) if row else {"forum": forum, "pages_done": 0, "done": 0, "updated_at": None}

    def set_backfill_progress(self, forum: str, pages_done: int, done: bool) -> None:
        self.conn.execute(
            """INSERT INTO backfill_progress (forum, pages_done, done, updated_at) VALUES (?,?,?,?)
               ON CONFLICT(forum) DO UPDATE SET pages_done=excluded.pages_done,
                 done=excluded.done, updated_at=excluded.updated_at""",
            (forum, pages_done, int(done), utcnow_iso()),
        )
        self.conn.commit()

    # --- housekeeping ---

    def counts(self) -> dict:
        out = {}
        for table in ("listings", "valuations", "roster", "comps_archive"):
            out[table] = self.conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        return out

    def size_bytes(self) -> int:
        total = self.path.stat().st_size
        for sidecar in self.path.parent.glob(self.path.name + "-*"):
            total += sidecar.stat().st_size
        return total

    def prune(self, retention_days: int) -> int:
        """Drop stale listings, archiving their price rows for comps first."""
        cutoff_sql = f"datetime('now', '-{int(retention_days)} days')"
        self.conn.execute("BEGIN")
        try:
            self.conn.execute(
                f"""INSERT OR IGNORE INTO comps_archive (source, external_id, forum, brand, model, price_usd, posted_at, archived_at)
                    SELECT source, external_id, forum, brand, model, price_usd, posted_at, ?
                      FROM listings
                     WHERE {_QUEUE_FRESH} < {cutoff_sql}""",
                (utcnow_iso(),),
            )
            cur = self.conn.execute(
                f"""DELETE FROM listings WHERE {_QUEUE_FRESH} < {cutoff_sql}
                     AND source || external_id IN (SELECT source || external_id FROM comps_archive)"""
            )
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        return cur.rowcount
