"""Typed config.yaml loader."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field


class SourceConfig(BaseModel):
    enabled: bool = True
    driver: Literal["http", "impersonate"] = "http"
    poll_interval: int = 300
    forums: list[str] | None = None
    categories: list[str] | None = None
    pages_per_poll: int = 1


class DiscordConfig(BaseModel):
    enabled: bool = True


class ImessageConfig(BaseModel):
    enabled: bool = False
    max_per_message: int = 6


class LlmConfig(BaseModel):
    enabled: bool = True  # needs LLM_API_KEY in .env; off = keywords only


class WatchConfig(BaseModel):
    """Deal-hunter geo filter: only listings near this origin get valued."""

    zip: str = "92122"
    radius_miles: float = 100.0


class ValuationConfig(BaseModel):
    enabled: bool = True
    model: str = "glm-5.3-flash"  # env VALUATION_MODEL wins
    thinking: bool = True
    web_search: bool = True       # needs the z.ai web-search MCP (same key)
    alert_min_score: int = 70     # deal alerts at/above this score
    max_per_cycle: int = 2        # valuations drained per watch cycle
    eval_window_days: int = 90    # only recently-active listings get valued
    sources: list[str] = Field(default_factory=lambda: ["calguns"])


class BackfillConfig(BaseModel):
    days: int = 90
    request_interval: float = 1.5  # seconds between calguns fetches (+jitter)
    forums: list[str] = Field(default_factory=lambda: ["handguns", "long_guns"])


class DbConfig(BaseModel):
    path: str = "armory.db"
    retention_days: int = 0  # 0 = keep everything; bodies are capped, growth is slow


class AlertsConfig(BaseModel):
    discord: DiscordConfig = DiscordConfig()
    imessage: ImessageConfig = ImessageConfig()
    max_per_cycle: int = 6  # overflow is summarized, not spammed


class Config(BaseModel):
    state_path: str = "armory.state.json"
    sources: dict[str, SourceConfig] = Field(default_factory=dict)
    keywords: list[str] = Field(default_factory=list)
    alerts: AlertsConfig = AlertsConfig()
    llm: LlmConfig = LlmConfig()
    watch: WatchConfig = WatchConfig()
    valuation: ValuationConfig = ValuationConfig()
    backfill: BackfillConfig = BackfillConfig()
    db: DbConfig = DbConfig()


def find_config_path(explicit: str | None = None) -> Path:
    if explicit:
        return Path(explicit)
    for cand in (Path.cwd() / "config.yaml", Path(__file__).parent.parent.parent / "config.yaml"):
        if cand.exists():
            return cand
    return Path.cwd() / "config.yaml"


def load_config(path: str | Path | None = None) -> Config:
    p = find_config_path(str(path) if path else None)
    if not p.exists():
        raise FileNotFoundError(f"config not found: {p} (expected config.yaml next to where you run armory)")
    data = yaml.safe_load(p.read_text()) or {}
    cfg = Config.model_validate(data)
    # anchor relative artifact paths to the config file's directory
    if not Path(cfg.state_path).is_absolute():
        cfg.state_path = str(p.resolve().parent / cfg.state_path)
    if not Path(cfg.db.path).is_absolute():
        cfg.db.path = str(p.resolve().parent / cfg.db.path)
    return cfg
