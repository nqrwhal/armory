"""Search: live queries against each site that supports them."""

from __future__ import annotations

from rich.console import Console
from rich.table import Table

from .adapters.base import AdapterError, SearchUnsupported, SourceAdapter
from .models import Listing

console = Console()


def search_remote(
    adapters: dict[str, SourceAdapter],
    query: str,
    source: str | None = None,
    limit: int = 25,
) -> list[Listing]:
    results: dict[tuple[str, str], Listing] = {}
    for name, adapter in adapters.items():
        if source and name != source:
            continue
        try:
            for listing in adapter.search(query, limit=limit):
                results.setdefault(listing.dedup_key, listing)
        except SearchUnsupported as exc:
            console.print(f"[yellow]({exc})[/yellow]")
        except AdapterError as exc:
            console.print(f"[yellow]({name} search unavailable: {exc})[/yellow]")
    return list(results.values())[:limit]


def print_results(listings: list[Listing]) -> None:
    if not listings:
        console.print("[dim]no results[/dim]")
        return
    table = Table(show_lines=False, header_style="bold")
    for col, kwargs in (
        ("source", {}),
        ("price", {"justify": "right"}),
        ("posted", {}),
        ("title", {"overflow": "fold", "ratio": 1}),
    ):
        table.add_column(col, **kwargs)
    for l in listings:
        posted = l.posted_at.strftime("%m-%d %H:%M") if l.posted_at else "?"
        table.add_row(l.source, l.price or "—", posted, l.title)
    console.print(table)
    for l in listings[:8]:
        console.print(f"  [link={l.url}]{l.url}[/link]")
    if len(listings) > 8:
        console.print(f"  [dim]…+{len(listings) - 8} more[/dim]")
