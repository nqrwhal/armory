"""Secrets loading from .env (simple KEY=VALUE lines, no external dep)."""

from __future__ import annotations

import os
from pathlib import Path


def load_env(explicit: str | None = None) -> None:
    """Populate os.environ from .env without overriding existing variables."""
    if explicit:
        paths = [Path(explicit)]
    else:
        paths = [Path.cwd() / ".env", Path(__file__).parent.parent.parent / ".env"]
    for p in paths:
        if not p.exists():
            continue
        for line in p.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip().strip("'\"")
            if key and key not in os.environ:
                os.environ[key] = value


def secret(name: str) -> str | None:
    val = os.environ.get(name, "").strip()
    return val or None


def parse_cookie_header(raw: str) -> dict[str, str]:
    """'a=1; b=2' -> {'a': '1', 'b': '2'}"""
    out: dict[str, str] = {}
    for part in raw.split(";"):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out
