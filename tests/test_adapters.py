from __future__ import annotations

from armory.adapters.caguns import CagunsAdapter
from armory.adapters.calguns import CalgunsAdapter, _thread_id
from armory.adapters.tacswap import TacswapAdapter, _parse_relative
from armory.config import SourceConfig


def make(adapter_cls, name="test"):
    cfg = SourceConfig()
    # Fetcher is never used when parsing fixtures directly
    return adapter_cls(cfg, fetcher=None)


class TestCalguns:
    def test_thread_id(self):
        assert _thread_id("https://www.calguns.net/forum/marketplace/private-firearms-sales-handguns/54996688-rossi-357") == "54996688"
        assert _thread_id("https://www.calguns.net/forum/marketplace/x/123456-slug/") == "123456"

    def test_parse_rss(self, fixture):
        import feedparser

        parsed = feedparser.parse(fixture("calguns_rss.xml"))
        assert parsed.entries, "fixture feed should have entries"
        adapter = make(CalgunsAdapter)

        listings = []
        from armory.models import Listing

        for entry in parsed.entries:
            from armory.adapters.calguns import _strip_html

            listings.append(
                Listing(
                    source="calguns",
                    external_id=_thread_id(entry["link"]),
                    url=entry["link"],
                    title=entry.get("title", ""),
                    body=_strip_html(entry.get("summary", "")),
                    author=entry.get("author"),
                )
            )
        assert listings
        assert all(l.title for l in listings)
        assert all(l.external_id.isdigit() for l in listings)
        assert any(l.posted_at is None or l.posted_at for l in listings)


class TestTacswap:
    def test_parse_page(self, fixture):
        adapter = make(TacswapAdapter)
        listings = adapter.parse_page(fixture("tacswap_category.html"), "firearm_accessories")
        assert len(listings) >= 10
        first = listings[0]
        assert first.source == "tacswap"
        assert first.external_id and len(first.external_id) == 24
        assert first.url.startswith("https://tacswap.com/post/")
        assert first.title
        assert first.price and first.price.startswith("$")
        assert first.author
        assert first.location and ", " in first.location
        assert first.image_url and first.image_url.startswith("https://")

    def test_parse_relative(self):
        assert _parse_relative("29 secs") is not None
        assert _parse_relative("5 mins") is not None
        assert _parse_relative("3 hours") is not None
        assert _parse_relative("2 days") is not None
        assert _parse_relative("1 week") is not None
        assert _parse_relative("2 months") is not None
        assert _parse_relative("pro badge") is None

    def test_action_id_discovery_regex(self, fixture):
        from armory.adapters.tacswap import _ACTION_REF

        html = fixture("tacswap_category.html")
        m = _ACTION_REF.search(html)
        assert m, "fixture page should embed the server-action reference"
        assert len(m.group(1)) >= 40 and all(c in "0123456789abcdef" for c in m.group(1))
        assert len(_ACTION_REF.findall(html)) == 1  # unique — no ambiguity

    def test_parse_flight_search(self, fixture):
        from armory.adapters.tacswap import _flight_listing_to_model, parse_flight_listings

        objs = parse_flight_listings(fixture("tacswap_search_flight.txt"))
        assert len(objs) >= 50
        seen_ids = set()
        for o in objs:
            assert o["id"] not in seen_ids  # deduped
            seen_ids.add(o["id"])
            assert o["itemTitle"]
        # map one full object to a Listing and check field extraction
        sample = next(o for o in objs if o["itemTitle"] == "G42 holster")
        listing = _flight_listing_to_model(sample)
        assert listing.source == "tacswap"
        assert listing.url == "https://tacswap.com/post/" + sample["id"]
        assert listing.price == "$30"
        assert listing.author == "jasonbruuu"
        assert listing.location and "NC" in listing.location
        assert "holster" in listing.body.lower()
        assert listing.image_url and listing.image_url.startswith("https://img.tacswap.com/")


class TestCaguns:
    def test_parse_page(self, fixture):
        adapter = make(CagunsAdapter)
        listings = adapter.parse_page(fixture("caguns_classifieds.html"))
        assert len(listings) >= 15
        first = listings[0]
        assert first.source == "caguns"
        assert first.external_id.isdigit()
        assert first.url.startswith("https://caguns.net/classifieds/")
        assert first.title
        assert first.price and first.price.startswith("$")
        assert first.author == "privateryry"
        assert first.posted_at is not None
        assert first.location and "SoCal" in first.location
        assert "Caliber" in first.body
        assert first.body.startswith("Engraved Receiver")  # description snippet first

    def test_block_detection(self, fixture):
        from armory.adapters.caguns import _classify_block

        assert "Cloudflare" in _classify_block("<html><title>Just a moment...</title></html>")
        assert "login wall" in _classify_block(
            "<html>You must be logged-in to do that. Please log in or register.</html>"
        )
        assert _classify_block(fixture("caguns_classifieds.html")) is None
