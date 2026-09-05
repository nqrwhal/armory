from __future__ import annotations

import pytest

from armory.matching import compile_term
from armory.models import Listing
from armory.state import StateStore


class TestStateStore:
    def test_roundtrip(self, tmp_path):
        state = StateStore(tmp_path / "s.json")
        assert state.is_first_run("calguns")
        state.add_keyword("glock")
        state.add_keyword("surefire")
        state.add_rule("cheap optics under $400")
        state.update_source("calguns", ["1", "2", "3"], "2026-09-02T00:00:00+00:00")
        state.save()

        again = StateStore(tmp_path / "s.json")
        assert not again.is_first_run("calguns")
        assert [k["term"] for k in again.keywords()] == ["glock", "surefire"]
        assert again.rules() == ["cheap optics under $400"]
        assert again.source("calguns").recent_ids == ["1", "2", "3"]

    def test_dupes_rejected(self, tmp_path):
        state = StateStore(tmp_path / "s.json")
        assert state.add_keyword("glock")
        assert not state.add_keyword("glock")
        assert state.add_rule("r1")
        assert not state.add_rule("r1")
        assert state.remove_rule("r1")
        assert not state.remove_rule("r1")

    def test_ring_trim(self, tmp_path):
        state = StateStore(tmp_path / "s.json")
        state.update_source("s", [str(i) for i in range(800)], "now")
        assert len(state.source("s").recent_ids) == 500

    def test_seed_only_fresh(self, tmp_path):
        state = StateStore(tmp_path / "s.json")
        assert state.seed_keywords(["a", "b"]) == 2
        assert state.seed_keywords(["c"]) == 0  # already has keywords


class TestMatching:
    def test_word_boundary(self):
        pat = compile_term("glock")
        assert pat.search("GLOCK 19 gen 5")
        assert pat.search("my glock,")
        assert not pat.search("glockbox")  # no boundary at end
        assert not pat.search("glockx")    # no boundary at end

    def test_regex(self):
        pat = compile_term(r"cmr\s*307", is_regex=True)
        assert pat.search("Holosun CMR307 brand new")

    def test_parse_term(self):
        from armory.matching import parse_term

        assert parse_term("glock") == ("glock", "include")
        assert parse_term("+glock") == ("glock", "include")
        assert parse_term("-airsoft") == ("airsoft", "exclude")
        assert parse_term("{trade}glock") == ("glock", "trade")

    def test_demand_side_suppression(self, tmp_path):
        from armory.matching import load_matchers, match_listing
        from armory.models import Listing

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        state.add_keyword("staccato", mode="trade")
        matchers = load_matchers(state)

        wtb = Listing(source="s", external_id="1", url="u", title="WTB Glock 19, cash ready")
        wtt = Listing(source="s", external_id="2", url="u", title="[WTT] my Glock for a Staccato")
        wts = Listing(source="s", external_id="3", url="u", title="WTS Glock 19 Gen 5")
        wtb_staccato = Listing(source="s", external_id="4", url="u", title="WTB Staccato HD cash in hand")

        assert match_listing(wtb, matchers) == []              # plain keyword ignores WTB
        assert match_listing(wtt, matchers) == ["staccato"]    # {trade} keyword matches demand side
        assert match_listing(wts, matchers) == ["glock"]       # normal sale matches
        assert match_listing(wtb_staccato, matchers) == ["staccato"]

    def test_explicit_wants_to_beats_heuristic(self, tmp_path):
        from armory.matching import detect_wants_to, load_matchers, match_listing
        from armory.models import Listing

        # LLM/source says wts even though title mentions WTB context
        listing = Listing(source="s", external_id="1", url="u", title="WTB vibes but actually selling this glock", wants_to="wts")
        assert detect_wants_to(listing) == "wts"
        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        assert match_listing(listing, load_matchers(state)) == ["glock"]

    def test_exclude_suppresses_everything(self, tmp_path):
        from armory.matching import load_matchers, match_listing
        from armory.models import Listing

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        state.add_keyword("-airsoft")
        matchers = load_matchers(state)

        hit = Listing(source="s", external_id="1", url="u", title="WTS Glock 19")
        noise = Listing(source="s", external_id="2", url="u", title="Glock airsoft replica")
        assert match_listing(hit, matchers) == ["glock"]
        assert match_listing(noise, matchers) == []  # exclude wins over include

    def test_back_compat_modeless_keywords(self, tmp_path):
        from armory.matching import load_matchers

        state = StateStore(tmp_path / "s.json")
        state.data["keywords"] = [{"term": "glock", "is_regex": False}]  # pre-mode entry
        matchers = load_matchers(state)
        assert matchers.include == [("glock", matchers.include[0][1], False)]

    def test_trade_keyword_replaces_plain(self, tmp_path):
        state = StateStore(tmp_path / "s.json")
        assert state.add_keyword("glock")
        assert state.add_keyword("{trade}glock")
        kws = state.keywords()
        assert len(kws) == 1 and kws[0]["mode"] == "trade"

    def test_load_matchers_from_state(self, tmp_path):
        from armory.matching import compile_term as ct
        from armory.matching import match_listing, load_matchers
        from armory.models import Listing

        state = StateStore(tmp_path / "s.json")
        state.add_keyword("glock")
        state.add_keyword(r"g\s*19", is_regex=True)
        matchers = load_matchers(state)
        listing = Listing(source="s", external_id="1", url="u", title="G19 for sale")
        assert match_listing(listing, matchers) == ["g\\s*19"]


class TestDiscordEmbed:
    def test_embed_shape_and_source_style(self):
        from armory.alerts.discord import _embed, _style

        listing = Listing(
            source="tacswap",
            external_id="x",
            url="https://tacswap.com/post/x",
            title="Holosun IRIS",
            price="$900",
            body="lasers",
            author="bigd",
            image_url="https://img.tacswap.com/x.jpg",
            matched_keywords=["holosun"],
        )
        e = _embed(listing)
        assert e["url"] == listing.url
        assert e["color"] == _style("tacswap")[1]
        assert e["author"]["name"] == "bigd"
        assert e["thumbnail"]["url"] == listing.image_url
        assert "$900" in e["description"]
        assert "holosun" in e["footer"]["text"]
        # unknown sources fall back to the generic identity
        assert _style("armory")[0] == "armory"
        assert _style("caguns")[0] == "armory · caguns"

    def test_send_groups_by_source(self):
        import httpx
        import respx

        from armory.alerts.discord import DiscordAlerter

        alerter = DiscordAlerter("https://discord.example/hook")
        listings = [
            Listing(source="calguns", external_id="1", url="u", title="a"),
            Listing(source="calguns", external_id="2", url="u", title="b"),
            Listing(source="tacswap", external_id="3", url="u", title="c"),
        ]
        sent: list[dict] = []

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            sent.append(json.loads(request.content))
            return httpx.Response(204)

        with respx.mock:
            respx.post("https://discord.example/hook").mock(side_effect=handler)
            alerter.send(listings, note="+2 more suppressed this cycle")

        assert len(sent) == 2  # one message per source
        by_user = {m["username"]: m for m in sent}
        assert by_user["armory · calguns"]["content"].startswith("2 new matches")
        assert by_user["armory · tacswap"]["content"].endswith("(+2 more suppressed this cycle)")
        assert len(by_user["armory · calguns"]["embeds"]) == 2
