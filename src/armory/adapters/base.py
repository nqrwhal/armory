"""Adapter interface: one class per source site."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..config import SourceConfig
from ..http import Fetcher
from ..models import HealthResult, Listing


class AdapterError(Exception):
    """Raised for auth expiry, anti-bot blocks, or unexpected page shapes."""


class SearchUnsupported(AdapterError):
    pass


class SourceAdapter(ABC):
    name: str = "source"

    def __init__(self, cfg: SourceConfig, fetcher: Fetcher):
        self.cfg = cfg
        self.fetcher = fetcher

    @abstractmethod
    def poll(self, pages: int = 1) -> list[Listing]: ...

    @abstractmethod
    def health(self) -> HealthResult: ...

    def enrich(self, listing: Listing) -> None:
        """Fill in description/metadata the poll listing lacks. Called once per
        NEW listing before keyword matching; keep it to at most one request."""
        return None

    def search(self, query: str, limit: int = 25) -> list[Listing]:
        raise SearchUnsupported(f"{self.name} has no remote search; using local DB")
