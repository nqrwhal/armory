"""Adapter registry: build enabled adapters from config + secrets."""

from __future__ import annotations

from ..config import Config, SourceConfig
from ..http import Fetcher
from ..secrets import parse_cookie_header, secret
from .base import AdapterError, SearchUnsupported, SourceAdapter
from .calguns import CalgunsAdapter
from .caguns import CagunsAdapter
from .gafshub import GafshubAdapter
from .tacswap import TacswapAdapter


def build_adapters(cfg: Config) -> dict[str, SourceAdapter]:
    adapters: dict[str, SourceAdapter] = {}
    for name, scfg in cfg.sources.items():
        if not scfg.enabled:
            continue
        try:
            adapters[name] = _build_one(name, scfg)
        except AdapterError as exc:
            # Missing credentials shouldn't take down the other sources; the
            # doctor/setup commands surface how to fix it.
            print(f"! {name}: {exc}")
    return adapters


def _build_one(name: str, scfg: SourceConfig) -> SourceAdapter:
    if name == "calguns":
        return CalgunsAdapter(scfg, Fetcher("http"))
    if name == "tacswap":
        return TacswapAdapter(scfg, Fetcher("http"))
    if name == "gafshub":
        api_key = secret("GAFSHUB_USER_API_KEY")
        if api_key:
            return GafshubAdapter(scfg, Fetcher("impersonate", headers={"User-Api-Key": api_key}))
        from .gafshub import load_jar

        jar = load_jar()  # persisted, keeps up with Discourse's rotating _t token
        cookie = secret("GAFSHUB_COOKIE")
        if jar is None and not cookie:
            raise AdapterError("no credential — set GAFSHUB_COOKIE in .env (see `armory setup`)")
        cookies = jar or parse_cookie_header(cookie)
        return GafshubAdapter(scfg, Fetcher("impersonate", cookies=cookies))
    if name == "caguns":
        cookies_raw = secret("CAGUNS_COOKIES")
        if not cookies_raw:
            raise AdapterError("no credential — set CAGUNS_COOKIES in .env (see `armory setup`)")
        return CagunsAdapter(scfg, Fetcher("impersonate", cookies=parse_cookie_header(cookies_raw)))
    raise AdapterError(f"unknown source '{name}'")
