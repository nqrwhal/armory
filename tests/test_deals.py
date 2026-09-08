"""Deal-hunter pipeline tests: scraper, DB, geo, roster, search MCP, valuation."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from armory.adapters.calguns import CalgunsAdapter, parse_title, parse_vb_date
from armory.config import SourceConfig
from armory.db import Db, sql_ts
from armory.geo import GeoResolver, distance_miles
from armory.models import ThreadRow
from armory.roster import RosterIndex, normalize_make, normalize_model

FIXTURES = Path(__file__).parent / "fixtures"


class FakeResp:
    def __init__(self, text, status=200):
        self.status_code = status
        self.text = text


class FakeFetcher:
    """Serves the captured calguns pages; records requested URLs."""

    def __init__(self):
        self.urls = []

    def get(self, url, params=None, retries=2, timeout=30.0):
        self.urls.append(url)
        import re

        # thread URLs end in /<id> or /<id>-slug; the forum list URL doesn't
        if re.search(r"/\d+(-[a-z0-9-]*)?$", url.rstrip("/")):
            return FakeResp((FIXTURES / "calguns_thread_page.html").read_text())
        return FakeResp((FIXTURES / "calguns_list_page.html").read_text())


def make_adapter() -> CalgunsAdapter:
    ad = CalgunsAdapter(SourceConfig(), FakeFetcher())
    ad._forum_urls["handguns"] = "https://www.calguns.net/forum/marketplace/private-firearms-sales-handguns"
    return ad


# --- scraper ---


def test_list_page_parses_rows_and_skips_stickies():
    rows = make_adapter().list_page("handguns", 1)
    assert len(rows) == 50
    assert all(r.external_id != "103986" for r in rows)  # the sticky thread
    first = rows[0]
    assert first.external_id == "54997724"
    assert first.title == "Glock 19"
    assert first.author == "aye_dre"
    assert first.posted_at is not None and first.posted_at.tzinfo is not None
    assert first.last_post_at >= first.posted_at
    assert first.replies == 2 and first.views == 386


def test_list_page_detects_markup_change():
    ad = CalgunsAdapter(SourceConfig(), FakeFetcher())
    ad._forum_urls["handguns"] = "https://x/forum"
    ad.fetcher.get = lambda *a, **k: FakeResp("<html>nothing here</html>")
    from armory.adapters.base import AdapterError

    with pytest.raises(AdapterError, match="markup changed"):
        ad.list_page("handguns", 1)


def test_thread_body_extracts_first_post():
    body, image_url = make_adapter().thread_body("https://x/54997724-glock-19")
    assert "Selling my Glock 19 Gen 3" in body
    assert "Trijicon RMR 3.25 MOA RM06" in body  # the attachment list matters for valuation


def test_parse_title_conventions():
    assert parse_title("WTS Glock 17 - Laguna Niguel - $700 OBO")["price_usd"] == 700.0
    assert parse_title("SOLD - S&W M&P15 $1,350")["sold"] is True
    assert parse_title("WTB Glock 19 - San Diego - $600")["wants_to"] == "wtb"
    assert parse_title("FS/FT Sig P365 $525.00")["wants_to"] == "wts"
    assert parse_title("Vintage Colt Python")["price_usd"] is None


def test_parse_vb_date_converts_board_tz_to_utc():
    dt = parse_vb_date("09-02-2026, 8:09 PM")  # America/Los_Angeles (PDT)
    assert dt.tzinfo == timezone.utc
    assert dt.isoformat().startswith("2026-09-03T03:09")


# --- db ---


def now_row(external_id="1", forum="handguns", days_ago=0, last_post=None, title="WTS Glock 19 $600"):
    return ThreadRow(
        source="calguns", forum=forum, external_id=external_id,
        url=f"https://x/{external_id}", title=title, author="s",
        posted_at=datetime.now(timezone.utc) - timedelta(days=days_ago),
        last_post_at=last_post or (datetime.now(timezone.utc) - timedelta(days=days_ago)),
    )


def test_upsert_new_changed_and_status():
    db = Db(":memory:")
    r = db.upsert_threads("calguns", [now_row("1", title="WTS Glock 19 $600")])
    assert len(r.new) == 1 and not r.changed
    listing = db.get_listing("calguns", "1")
    assert listing["price_usd"] == 600.0 and listing["wants_to"] == "wts" and listing["status"] == "open"

    r = db.upsert_threads("calguns", [now_row("1", title="WTS Glock 19 $550 price drop")])
    assert not r.new and len(r.changed) == 1
    assert db.get_listing("calguns", "1")["price_usd"] == 550.0

    db.upsert_threads("calguns", [now_row("1", title="SOLD Glock 19")])
    assert db.get_listing("calguns", "1")["status"] == "sold"


def test_valuation_queue_filters_and_requeues():
    db = Db(":memory:")
    origin_lat, origin_lon = 32.8568, -117.2102  # 92122

    def row(external_id, miles_city, forum="handguns", days_ago=1, title="WTS Glock 19 $600"):
        return now_row(external_id, forum=forum, days_ago=days_ago, title=title), miles_city

    near = {"El Cajon": 15.0, "Laguna Niguel": 54.0}
    rows = [row("near1", "El Cajon"), row("near2", "Laguna Niguel"),
            row("far1", "Fresno"), row("old1", "El Cajon", days_ago=120),
            row("wtb1", "El Cajon", title="WTB Glock 19 $600"),
            row("lg1", "El Cajon", forum="long_guns", title="WTS Ruger 10/22 $400"),
            row("noprice", "El Cajon", title="WTS Glock 19 make offer")]
    for r, city in rows:
        db.upsert_threads("calguns", [r])
    for external_id, city in [("near1", "El Cajon"), ("near2", "Laguna Niguel"),
                              ("far1", "Fresno"), ("old1", "El Cajon"),
                              ("wtb1", "El Cajon"), ("lg1", "El Cajon")]:
        hit = GeoResolver().resolve(city)
        db.set_geo("calguns", external_id, hit.lat, hit.lon,
                   distance_miles(origin_lat, origin_lon, hit.lat, hit.lon),
                   hit.quality, city, None, None)

    queue = {q["external_id"] for q in db.valuation_queue(90, 100.0)}
    assert "near1" in queue and "near2" in queue and "lg1" in queue
    assert "far1" not in queue      # outside radius
    assert "old1" not in queue      # outside eval window
    assert "wtb1" not in queue      # not a sale
    assert "noprice" not in queue   # nothing to value against

    # a valuation freezes the listing; a >5% price move re-queues it
    db.insert_valuation("calguns", "near1", {"asking_price": 600.0, "deal_score": 80, "verdict": "good"}, "test")
    assert "near1" not in {q["external_id"] for q in db.valuation_queue(90, 100.0)}
    db.upsert_threads("calguns", [now_row("near1", title="WTS Glock 19 $700")])
    assert "near1" in {q["external_id"] for q in db.valuation_queue(90, 100.0)}


def test_comps_and_backfill_progress():
    db = Db(":memory:")
    db.upsert_threads("calguns", [
        now_row("a", title="WTS Glock 19 $600"), now_row("b", title="WTS Glock 19 $650"),
    ])
    db.set_llm_fields("calguns", "a", {"brand": "Glock", "model": "19"})
    db.set_llm_fields("calguns", "b", {"brand": "Glock", "model": "19"})
    comps = db.comps("Glock", "19", ("calguns", "a"))
    assert [c["external_id"] for c in comps] == ["b"]
    assert db.backfill_progress("handguns")["pages_done"] == 0
    db.set_backfill_progress("handguns", 12, False)
    assert db.backfill_progress("handguns")["pages_done"] == 12


def test_prune_archives_price_history():
    db = Db(":memory:")
    db.upsert_threads("calguns", [
        now_row("old1", days_ago=400, title="WTS Glock 19 $600"),
        now_row("new1", days_ago=1, title="WTS Glock 19 $650"),
    ])
    db.set_llm_fields("calguns", "old1", {"brand": "Glock", "model": "19"})
    removed = db.prune(retention_days=365)
    assert removed == 1
    assert db.get_listing("calguns", "old1") is None
    assert db.get_listing("calguns", "new1") is not None
    archived = db.conn.execute("SELECT price_usd FROM comps_archive WHERE external_id='old1'").fetchone()
    assert archived["price_usd"] == 600.0


# --- geo ---


def test_geo_resolution_priorities():
    g = GeoResolver()
    hit = g.resolve("FTF in North Park, San Diego 92104")
    assert hit.quality == "zip" and hit.label == "92104"
    hit = g.resolve("WTS Glock 17 Laguna Niguel $700")
    assert hit.quality == "place" and "Laguna Niguel" in hit.label
    hit = g.resolve("somewhere in NorCal")
    assert hit.quality == "region"
    assert g.resolve("no place at all in this text zzz") is None


def test_geo_distance_radius():
    g = GeoResolver()
    origin = g.origin("92122")
    sd = g.resolve("San Diego 92101")
    la = g.resolve("Los Angeles, CA 90012")
    assert distance_miles(origin.lat, origin.lon, sd.lat, sd.lon) < 20
    assert 90 < distance_miles(origin.lat, origin.lon, la.lat, la.lon) < 130


# --- roster ---


def test_roster_normalization_and_lookup():
    assert normalize_make("Smith & Wesson") == normalize_make("S&W")
    assert normalize_model("G19X", "Glock") == "19x"
    assert normalize_model("P365", "SIG Sauer") == "p365"
    db = Db(":memory:")
    db.roster_load([
        ("glock", "19", "on", "Glock", "19"),
        ("glock", "19x", "off", "Glock", "19X"),
        ("sigsauer", "3659bxr3pmsca", "on", "SIG Sauer", "365-9-BXR3P-MS-CA"),
    ])
    idx = RosterIndex(db)
    assert idx.lookup("Glock", "19 Gen 3") == "on"
    assert idx.lookup("Glock", "19X") == "off"       # exact beats prefix
    assert idx.lookup("SIG Sauer", "P365") == "on"   # loose prefix on SKU strings
    assert idx.lookup("Kimber", "Whatever") == "unknown"


# --- z.ai search MCP client ---


def test_zai_search_handshake_and_call(respx_mock):
    import httpx

    from armory.zai_search import ZaiSearch

    base = "https://mcp.test/mcp"
    responses = [
        httpx.Response(  # initialize → session id
            200,
            headers={"content-type": "application/json", "mcp-session-id": "sess-1"},
            json={"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "z"}}},
        ),
        httpx.Response(202, headers={"content-type": "application/json"}),  # initialized notification
        httpx.Response(  # tools/call result
            200,
            headers={"content-type": "application/json"},
            json={"jsonrpc": "2.0", "id": 3, "result": {"content": [
                {"type": "text", "text": "search-result: glock 19 used $450"}
            ]}},
        ),
    ]
    respx_mock.post(base).mock(side_effect=lambda request: responses.pop(0))
    zs = ZaiSearch(url=base, api_key="k")
    out = zs.search("glock price")
    assert "search-result" in out
    request_bodies = [json.loads(c.request.content) for c in respx_mock.calls]
    assert request_bodies[0]["method"] == "initialize"
    assert request_bodies[1]["method"] == "notifications/initialized"
    call = request_bodies[2]
    assert call["method"] == "tools/call"
    assert call["params"]["name"] == "web_search_prime"
    assert call["params"]["arguments"]["location"] == "us"
    assert any(h == "mcp-session-id" and v == "sess-1" for h, v in respx_mock.calls[2].request.headers.multi_items())


def test_zai_search_sse_stream(respx_mock):
    import httpx

    from armory.zai_search import ZaiSearch

    base = "https://mcp.test/mcp"
    sse = (
        'event: message\n'
        'data: {"jsonrpc":"2.0","id":1,"result":{"serverInfo":{}}}\n\n'
    )
    respx_mock.post(base).mock(
        return_value=httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=sse
        )
    )
    zs = ZaiSearch(url=base, api_key="k")
    assert zs.tools() == []  # parsed from the SSE data line


# --- valuation engine (fake LLM + fake search) ---


class FakeLLM:
    """Scripted: classify fills identity; tool loop returns a canned verdict."""

    configured = True
    anthropic = True
    model = "fake"

    def __init__(self, verdict):
        self.verdict = verdict
        self.loop_calls = 0

    def classify_batch(self, listings, rules):
        return {l.external_id: {"brand": "Glock", "model": "19", "item_type": "firearm"} for l in listings}

    def run_tool_loop(self, system, user, tools, executor, model=None, thinking=None, max_rounds=4):
        self.loop_calls += 1
        assert tools, "web_search tool should be offered"
        out = executor("web_search", {"query": "glock 19 price"})
        assert "search-result" in out
        return json.dumps(self.verdict)


class FakeSearch:
    configured = True

    def search(self, query, recency=None):
        return "search-result: glock 19 used $450"


class RecordingAlerters:
    def __init__(self):
        self.sent = []

    def send_all(self, listings, note=None):
        self.sent.extend(listings)
        return []

    def enabled(self):
        return ["fake"]


def _seed_valuation_db():
    db = Db(":memory:")
    db.roster_load([("glock", "19", "on", "Glock", "19")])
    db.upsert_threads("calguns", [now_row("1", title="WTS Glock 19 $600")])
    hit = GeoResolver().resolve("El Cajon")
    db.set_geo("calguns", "1", hit.lat, hit.lon,
               distance_miles(32.8568, -117.2102, hit.lat, hit.lon), hit.quality, "El Cajon", None, None)
    return db


def _verdict(score, verdict="good", asking=600.0):
    return {
        "make": "Glock", "model": "19", "confidence": "high",
        "attachments": [{"item": "3 mags", "est_value_usd": 90}],
        "market_low": 500, "market_mid": 650, "market_high": 800,
        "value_basis": "web + comps", "ca_roster": "on", "off_roster_premium_pct": None,
        "deal_score": score, "verdict": verdict, "summary": "Solid deal.",
        "caveats": None, "scam_risk": "low", "sources": ["https://guns.com/x"],
        "asking_price": asking,
    }


def test_valuation_alerts_on_good_deal():
    from armory.config import ValuationConfig
    from armory.valuation import ValuationEngine

    db = _seed_valuation_db()
    llm = FakeLLM(_verdict(82))
    alerters = RecordingAlerters()
    engine = ValuationEngine(db, llm, ValuationConfig(), search=FakeSearch(),
                             roster=RosterIndex(db), alerters=alerters, radius_miles=100)
    stats = engine.run(limit=5)
    assert stats == {"queued": 1, "valued": 1, "alerted": 0, "errors": 0}
    assert llm.loop_calls == 1
    assert len(alerters.sent) == 1
    sent = alerters.sent[0]
    assert "🔥 DEAL 82/100" in sent.title
    assert "OFF-ROSTER" not in sent.body  # roster 'on' → no premium banner
    v = db.latest_valuation("calguns", "1")
    assert v["deal_score"] == 82 and v["alerted_at"]
    # queued exactly once: valuation exists now
    assert db.valuation_queue(90, 100.0) == []


def test_valuation_no_alert_below_threshold_or_bad_verdict():
    from armory.config import ValuationConfig
    from armory.valuation import ValuationEngine

    for verdict in (_verdict(40, verdict="fair"), _verdict(95, verdict="overpriced")):
        db = _seed_valuation_db()
        alerters = RecordingAlerters()
        engine = ValuationEngine(db, FakeLLM(verdict), ValuationConfig(), search=FakeSearch(),
                                 roster=RosterIndex(db), alerters=alerters, radius_miles=100)
        engine.run(limit=5)
        assert not alerters.sent


def test_valuation_realert_only_on_price_drop():
    from armory.config import ValuationConfig
    from armory.valuation import ValuationEngine

    db = _seed_valuation_db()
    alerters = RecordingAlerters()
    engine = ValuationEngine(db, FakeLLM(_verdict(80)), ValuationConfig(), search=FakeSearch(),
                             roster=RosterIndex(db), alerters=alerters, radius_miles=100)
    engine.run(limit=5)
    assert len(alerters.sent) == 1
    # price drop > 5% re-queues and re-alerts
    db.upsert_threads("calguns", [now_row("1", title="WTS Glock 19 $500")])
    engine.run(limit=5)
    assert len(alerters.sent) == 2
    # same price re-queued (e.g. forced) does NOT re-alert
    db.conn.execute("DELETE FROM valuations WHERE error IS NULL AND id > 2")
    engine.run(limit=5)
    assert len(alerters.sent) == 2


# --- pipeline ingest ---


def test_ingest_thread_rows_end_to_end():
    from armory.pipeline import ingest_thread_rows

    ad = make_adapter()
    db = Db(":memory:")
    resolver = GeoResolver()
    origin = resolver.origin("92122")
    rows = ad.list_page("handguns", 1)[:5]
    stats = ingest_thread_rows(ad, db, rows, resolver, origin, body_cap=3, throttle=0)
    assert stats["rows"] == 5 and stats["new"] == 5
    assert stats["bodies"] == 3  # capped; the rest deferred
    # a Laguna Niguel listing from the fixture should be geo-located within radius
    laguna = db.conn.execute(
        "SELECT distance_miles, geo_quality FROM listings WHERE title LIKE '%Laguna Niguel%' LIMIT 1"
    ).fetchone()
    assert laguna is not None
    assert laguna["geo_quality"] in ("place", "zip")
    assert laguna["distance_miles"] < 100
