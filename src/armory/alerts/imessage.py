"""iMessage alerts via AppleScript → Messages.app (macOS only).

Requires Messages.app signed in and a one-time Automation permission grant
when armory first sends. The recipient must be reachable by iMessage at the
email/phone configured in IMESSAGE_TO.
"""

from __future__ import annotations

import subprocess

from ..models import Listing


def _applescape(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


class ImessageAlerter:
    def __init__(self, to: str | None, max_per_message: int = 6):
        self.to = to
        self.max_per_message = max_per_message

    @property
    def configured(self) -> bool:
        return bool(self.to)

    def _send_applescript(self, body: str) -> None:
        # AppleScript string literals have no \n escape; splice newlines in as
        # `" & linefeed & "` concatenations after quoting the text safely.
        escaped = _applescape(body).replace("\n", '" & linefeed & "')
        script = (
            f'tell application "Messages"\n'
            f'send "{escaped}" to buddy "{_applescape(self.to)}"\n'
            f"end tell"
        )
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"iMessage send failed: {result.stderr.strip()[:300]}")

    def send(self, listings: list[Listing], note: str | None = None) -> None:
        """One iMessage per poll cycle, bundling up to max_per_message listings."""
        if not self.configured or not listings:
            return
        lines: list[str] = []
        for listing in listings[: self.max_per_message]:
            price = f" — {listing.price}" if listing.price else ""
            lines.append(f"[{listing.source}] {listing.title}{price}\n{listing.url}")
        if len(listings) > self.max_per_message:
            lines.append(f"(+{len(listings) - self.max_per_message} more)")
        if note:
            lines.append(note)
        self._send_applescript("\n\n".join(lines))
