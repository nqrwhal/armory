"""Alert dispatch: fan matched listings out to enabled channels."""

from __future__ import annotations

from ..config import AlertsConfig
from ..models import Listing
from ..secrets import secret
from .discord import DiscordAlerter
from .imessage import ImessageAlerter


class Alerters:
    def __init__(self, cfg: AlertsConfig):
        self.discord = DiscordAlerter(secret("DISCORD_WEBHOOK_URL")) if cfg.discord.enabled else None
        self.imessage = (
            ImessageAlerter(secret("IMESSAGE_TO"), cfg.imessage.max_per_message)
            if cfg.imessage.enabled
            else None
        )

    def enabled(self) -> list[str]:
        names = []
        if self.discord and self.discord.configured:
            names.append("discord")
        if self.imessage and self.imessage.configured:
            names.append("imessage")
        return names

    def send_all(self, listings: list[Listing], note: str | None = None) -> list[str]:
        """Send to every configured channel; returns per-channel error strings."""
        errors: list[str] = []
        if not listings:
            return errors
        if self.discord and self.discord.configured:
            try:
                self.discord.send(listings, note=note)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"discord: {exc}")
        if self.imessage and self.imessage.configured:
            try:
                self.imessage.send(listings, note=note)
            except Exception as exc:  # noqa: BLE001
                errors.append(f"imessage: {exc}")
        return errors
