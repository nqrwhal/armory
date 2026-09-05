"""Discord webhook alerts with rich embeds, one visual identity per source."""

from __future__ import annotations

import httpx

from ..models import Listing

WEBHOOK_MAX_EMBEDS = 10

# source -> (username shown in Discord, embed accent color)
SOURCE_STYLES: dict[str, tuple[str, int]] = {
    "calguns": ("armory · calguns", 0xD9534F),
    "tacswap": ("armory · tacswap", 0x1DA1F3),
    "gafshub": ("armory · gafshub", 0x2ECC71),
    "caguns": ("armory · caguns", 0x9B59B6),
}
DEFAULT_STYLE = ("armory", 0x95A5A6)


def _style(source: str) -> tuple[str, int]:
    return SOURCE_STYLES.get(source, DEFAULT_STYLE)


def _embed(listing: Listing) -> dict:
    desc_lines = []
    if listing.price:
        desc_lines.append(f"**{listing.price}**")
    bits = [b for b in (listing.category, listing.location) if b]
    spec = " · ".join(b for b in (listing.brand, listing.model, listing.condition) if b)
    if spec:
        bits.append(spec)
    if bits:
        desc_lines.append(" · ".join(bits))
    if listing.scam_risk in ("medium", "high") and listing.scam_reason:
        desc_lines.append(f":warning: {listing.scam_risk} scam risk — {listing.scam_reason}")
    if listing.body:
        desc_lines.append(listing.body[:300] + ("…" if len(listing.body) > 300 else ""))
    embed: dict = {
        "title": (f"⚠️ {listing.title}" if listing.scam_risk == "high" else listing.title)[:256],
        "url": listing.url,
        "description": "\n".join(desc_lines)[:4096],
        "color": 0xE74C3C if listing.scam_risk == "high" else _style(listing.source)[1],
    }
    if listing.author:
        embed["author"] = {"name": listing.author}
    if listing.image_url:
        embed["thumbnail"] = {"url": listing.image_url}
    footer_bits = [listing.source]
    if listing.matched_keywords:
        footer_bits.append("keywords: " + ", ".join(listing.matched_keywords))
    if listing.matched_rules:
        footer_bits.append(f"rule{'s' if len(listing.matched_rules) > 1 else ''} matched")
    embed["footer"] = {"text": " · ".join(footer_bits)[:2048]}
    return embed


class DiscordAlerter:
    def __init__(self, webhook_url: str | None):
        self.webhook_url = webhook_url
        self.client = httpx.Client(timeout=15.0)

    @property
    def configured(self) -> bool:
        return bool(self.webhook_url)

    def send(self, listings: list[Listing], note: str | None = None) -> None:
        """One Discord message per source, each stamped with that source's identity."""
        if not self.configured or not listings:
            return
        by_source: dict[str, list[Listing]] = {}
        for listing in listings:
            by_source.setdefault(listing.source, []).append(listing)
        for source, batch in by_source.items():
            username, _ = _style(source)
            for i in range(0, len(batch), WEBHOOK_MAX_EMBEDS):
                chunk = batch[i : i + WEBHOOK_MAX_EMBEDS]
                content = f"{len(chunk)} new match" + ("es" if len(chunk) > 1 else "")
                if note:
                    content += f" ({note})"
                resp = self.client.post(
                    self.webhook_url,
                    json={
                        "username": username,
                        "content": content,
                        "embeds": [_embed(l) for l in chunk],
                    },
                )
                resp.raise_for_status()

    def close(self) -> None:
        self.client.close()
