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
    state_path = Path(cfg.state_path)
    if not state_path.is_absolute():
        cfg.state_path = str(p.resolve().parent / state_path)
    return cfg
