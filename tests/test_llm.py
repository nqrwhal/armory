from __future__ import annotations

import pytest

from armory.llm import LLMError, apply_result, extract_json
from armory.models import Listing, HealthResult


class TestExtractJson:
    def test_plain(self):
        assert extract_json('{"results": []}') == {"results": []}

    def test_fenced(self):
        text = 'Here you go:\n```json\n{"results": [{"id": "a"}]}\n```\nDone.'
        assert extract_json(text) == {"results": [{"id": "a"}]}

    def test_prose_wrapped(self):
        text = 'Sure! {"results": [{"id": "x", "note": "brace } inside string"}]} hope that helps'
        assert extract_json(text)["results"][0]["id"] == "x"

    def test_garbage_raises(self):
        with pytest.raises(LLMError):
            extract_json("no json here at all")
        with pytest.raises(LLMError):
            extract_json('{"unterminated')


class TestApplyResult:
    def test_merge(self):
        listing = Listing(source="s", external_id="1", url="u", title="t")
        apply_result(
            listing,
            {
                "id": "1",
                "brand": "Glock",
                "model": "19 Gen 5",
                "item_type": "firearm",
                "price_usd": "750.0",
                "condition": "like new",
                "wants_to": "wts",
                "scam_risk": "low",
                "matched_rules": ["anything Glock 19 related"],
            },
        )
        assert listing.brand == "Glock"
        assert listing.price_usd == 750.0
        assert listing.wants_to == "wts"
        assert listing.matched_rules == ["anything Glock 19 related"]
        assert listing.scam_risk == "low"

    def test_bad_values_ignored(self):
        listing = Listing(source="s", external_id="1", url="u", title="t")
        apply_result(listing, {"id": "1", "price_usd": "free", "scam_risk": "maybe", "wants_to": "yolo"})
        assert listing.price_usd is None
        assert listing.scam_risk is None
        assert listing.wants_to is None


class TestPollerState:
    """Full poll flow against the JSON state store with a fake adapter + LLM."""

    @staticmethod
    def make_adapter(ids):
        class FakeAdapter:
            name = "fake"

            def poll(self, pages=1):
                return [
                    Listing(source="fake", external_id=i, url=f"u{i}", title=f"Glock item {i}", body="b")
                    for i in ids
                ]

            def enrich(self, listing):
                return None

            def health(self):
                return HealthResult(True, "ok")

        return FakeAdapter()

    def test_first_run_seeds_silently_then_alerts_on_new(self, tmp_path):
        from armory.alerts import Alerters
        armory_state = tmp_path / "s.json"
        from armory.config import AlertsConfig
        from armory.poller import poll_source
        from armory.state import StateStore

        class FakeLLM:
            configured = True

            def classify_batch(self, listings, rules):
                return {l.external_id: {"id": l.external_id, "brand": "Glock"} for l in listings}

        state = StateStore(armory_state)
        state.add_keyword("glock")
        alerters = Alerters(AlertsConfig())  # no channels configured

        # first run: seed only, no alerts, no classification
        stats = poll_source(state, self.make_adapter(["1", "2"]), alerters, llm=FakeLLM())
        assert stats["new"] == 0 and stats["matched"] == 0

        # second run same ids: nothing new
        stats = poll_source(state, self.make_adapter(["1", "2"]), alerters, llm=FakeLLM())
        assert stats["new"] == 0

        # third run with a new id: detected, classified, matched
        stats = poll_source(state, self.make_adapter(["1", "2", "3"]), alerters, llm=FakeLLM())
        assert stats["new"] == 1
        assert stats["matched"] == 1  # keyword glock

    def test_bumped_listing_never_realerts(self, tmp_path):
        """A match that re-enters the feed after the ring rotates out must not
        re-alert, must skip the LLM, and must be visible in stats as a reseen."""
        from armory.alerts import Alerters
        from armory.config import AlertsConfig
        from armory.poller import poll_source
        from armory.state import RING_SIZE, StateStore

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        llm_calls: list[list] = []

        class TrackingLLM:
            configured = True

            def classify_batch(self, listings, rules):
                llm_calls.append([l.external_id for l in listings])
                return {}

        sent: list = []

        class CaptureAlerters(Alerters):
            def __init__(self):
                super().__init__(AlertsConfig())

            def send_all(self, listings, note=None):
                sent.append([l.external_id for l in listings])
                return []

        # seed run
        poll_source(state, self.make_adapter(["seed"]), Alerters(AlertsConfig()))
        # matching run: listing "dup" alerts
        stats = poll_source(state, self.make_adapter(["dup"]), CaptureAlerters(), llm=TrackingLLM())
        assert stats["matched"] == 1 and sent == [["dup"]]
        assert llm_calls == [["dup"]]  # LLM saw it once
        # push "dup" out of the ring, then it re-enters the feed (bump)
        state.update_source("fake", [str(i) for i in range(RING_SIZE + 10)], state.source("fake").last_check)
        state.save()
        stats = poll_source(state, self.make_adapter(["dup"]), CaptureAlerters(), llm=TrackingLLM())
        assert stats["new"] == 1          # detected again (bump)…
        assert stats["reseen"] == 1       # …but recognized as already alerted
        assert stats["matched"] == 0
        assert sent == [["dup"]]          # no new alert
        assert llm_calls == [["dup"]]     # no wasted LLM call

    def test_alert_cap_suppresses_overflow(self, tmp_path):
        from armory.alerts import Alerters, DiscordAlerter
        from armory.config import AlertsConfig
        from armory.poller import poll_source
        from armory.state import StateStore

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        poll_source(state, self.make_adapter(["seed"]), Alerters(AlertsConfig()))  # seed

        sent: list = []

        class CaptureAlerters(Alerters):
            def __init__(self):
                super().__init__(AlertsConfig())

            def send_all(self, listings, note=None):
                sent.append((listings, note))
                return []

        adapter = self.make_adapter([str(i) for i in range(100, 110)])  # 10 new matches
        stats = poll_source(state, adapter, CaptureAlerters(), max_alerts=6)
        assert stats["matched"] == 10
        assert stats["suppressed"] == 4
        listings, note = sent[0]
        assert len(listings) == 6
        assert note == "+4 more suppressed this cycle"

    def test_old_listing_ignored_after_ring_wrap(self, tmp_path):
        """Once an ID falls out of the ring, the last-check cutoff stops re-alerts."""
        from datetime import datetime, timedelta, timezone

        from armory.alerts import Alerters
        from armory.config import AlertsConfig
        from armory.poller import poll_source
        from armory.state import RING_SIZE, StateStore

        old = datetime.now(timezone.utc) - timedelta(days=3)

        class OldAdapter:
            name = "old"

            def poll(self, pages=1):
                return [
                    Listing(
                        source="old", external_id="zz", url="u", title="Glock ancient",
                        posted_at=old,
                    )
                ]

            def enrich(self, listing):
                return None

            def health(self):
                return HealthResult(True, "ok")

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        poll_source(state, OldAdapter(), Alerters(AlertsConfig()))  # seed

        # push zz out of the ring, then present it again: cutoff must ignore it
        state.update_source("old", [str(i) for i in range(RING_SIZE + 10)], state.source("old").last_check)
        state.save()
        stats = poll_source(state, OldAdapter(), Alerters(AlertsConfig()))
        assert stats["new"] == 0


class TestDiscordScamEmbed:
    def test_high_risk_styling(self):
        from armory.alerts.discord import _embed

        listing = Listing(
            source="tacswap", external_id="x", url="u", title="Cheap ACOG",
            scam_risk="high", scam_reason="price too good to be true",
            matched_rules=["optics under $400"],
        )
        e = _embed(listing)
        assert e["title"].startswith("⚠️")
        assert e["color"] == 0xE74C3C
        assert "too good" in e["description"]
        assert "rule matched" in e["footer"]["text"]
