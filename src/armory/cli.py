"""armory CLI."""

from __future__ import annotations

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .adapters import build_adapters
from .alerts import Alerters
from .config import load_config
from .models import Listing
from .poller import run_poll_cycle, watch
from .search import print_results, search_remote
from .secrets import load_env, secret
from .state import StateStore

app = typer.Typer(help="Personal listing monitor & search across firearm marketplaces.", no_args_is_help=True)
keywords_app = typer.Typer(help="Manage alert keywords.", no_args_is_help=True)
app.add_typer(keywords_app, name="keywords")
rules_app = typer.Typer(help="Manage natural-language alert rules (LLM).", no_args_is_help=True)
app.add_typer(rules_app, name="rules")
console = Console()


def _boot(config: str | None = None):
    load_env()
    cfg = load_config(config)
    adapters = build_adapters(cfg)
    alerters = Alerters(cfg.alerts)
    from .llm import LLMClient

    llm = LLMClient() if cfg.llm.enabled else None
    return cfg, adapters, alerters, llm


def _state(cfg) -> StateStore:
    state = StateStore(cfg.state_path)
    if state.seed_keywords(cfg.keywords):
        state.save()
    return state


@app.command(name="watch")
def watch_cmd(
    config: str = typer.Option(None, "--config", "-c"),
):
    """Poll all enabled sources forever and alert on keyword/rule matches."""
    cfg, adapters, alerters, llm = _boot(config)
    if not adapters:
        console.print("[red]no enabled sources — check config.yaml and .env[/red]")
        raise typer.Exit(1)
    state = _state(cfg)
    intervals = {name: cfg.sources[name].poll_interval for name in adapters}
    watch(
        state, adapters, alerters, intervals, llm=llm,
        max_alerts=cfg.alerts.max_per_cycle,
    )


@app.command()
def poll(
    source: str = typer.Option(None, "--source", "-s", help="Only poll this source"),
    config: str = typer.Option(None, "--config", "-c"),
):
    """One-shot poll of all (or one) enabled source(s)."""
    cfg, adapters, alerters, llm = _boot(config)
    if not adapters:
        console.print("[red]no enabled sources[/red]")
        raise typer.Exit(1)
    if source and source not in adapters:
        console.print(f"[red]unknown/disabled source '{source}' (available: {', '.join(adapters)})[/red]")
        raise typer.Exit(1)
    state = _state(cfg)
    ok = run_poll_cycle(state, adapters, alerters, llm=llm, only=source, max_alerts=cfg.alerts.max_per_cycle)
    raise typer.Exit(0 if ok else 1)


@app.command()
def search(
    query: str = typer.Argument(...),
    source: str = typer.Option(None, "--source", "-s"),
    limit: int = typer.Option(25, "--limit", "-l"),
    config: str = typer.Option(None, "--config", "-c"),
):
    """Search live on each site that supports it (no local history is kept)."""
    cfg, adapters, _, _ = _boot(config)
    results = search_remote(adapters, query, source=source, limit=limit)
    print_results(results)


@app.command()
def status(config: str = typer.Option(None, "--config", "-c")):
    """Show state: last check per source, keyword and rule counts."""
    cfg = load_config(config)
    state = StateStore(cfg.state_path)
    table = Table(header_style="bold")
    table.add_column("source")
    table.add_column("last check")
    table.add_column("ids remembered")
    for name in sorted(state.data["sources"]):
        s = state.source(name)
        table.add_row(name, s.last_check or "never", str(len(s.recent_ids)))
    console.print(table)
    console.print(f"[dim]{len(state.keywords())} keyword(s), {len(state.rules())} rule(s)[/dim]")


# --- keywords ---


@keywords_app.command("add")
def keywords_add(
    term: str = typer.Argument(...),
    regex: bool = typer.Option(False, "--regex", help="Treat TERM as a regex"),
    exclude: bool = typer.Option(False, "--exclude", "-x", help="Never alert on listings matching TERM"),
    trades: bool = typer.Option(False, "--trades", "-t", help="Also match WTT/WTB listings (demand side)"),
    config: str = typer.Option(None, "--config", "-c"),
):
    """Add an alert keyword (case-insensitive whole-word match unless --regex).

    Prefix syntax also works in TERM itself: -term (exclude), +term (include),
    {trade}term (include + WTT/WTB). WTT/WTB listings never alert on plain
    keywords — use --trades / {trade} to opt in.
    """
    import re as _re

    mode = "exclude" if exclude else ("trade" if trades else None)
    if regex:
        try:
            _re.compile(term.lstrip("-+").replace("{trade}", ""), _re.IGNORECASE)
        except _re.error as exc:
            console.print(f"[red]invalid regex: {exc}[/red]")
            raise typer.Exit(1)
    cfg = load_config(config)
    state = _state(cfg)
    if state.add_keyword(term, is_regex=regex, mode=mode):
        state.save()
        from .matching import parse_term

        clean, parsed = parse_term(term)
        shown_mode = mode or parsed
        console.print(f"[green]added[/green] {clean!r} ({shown_mode})" + (" regex" if regex else ""))
    else:
        console.print(f"[yellow]already exists[/yellow] {term!r}")


@keywords_app.command("remove")
def keywords_remove(
    term: str = typer.Argument(...),
    config: str = typer.Option(None, "--config", "-c"),
):
    cfg = load_config(config)
    state = _state(cfg)
    if state.remove_keyword(term):
        state.save()
        console.print("[green]removed[/green]")
    else:
        console.print("[yellow]not found[/yellow]")


@keywords_app.command("list")
def keywords_list(config: str = typer.Option(None, "--config", "-c")):
    cfg = load_config(config)
    state = StateStore(cfg.state_path)
    kws = state.keywords()
    if not kws:
        console.print("[dim]no keywords — add some with `armory keywords add <term>`[/dim]")
        return
    table = Table(header_style="bold")
    table.add_column("term")
    table.add_column("mode")
    table.add_column("type")
    for k in kws:
        table.add_row(k["term"], k.get("mode", "include"), "regex" if k.get("is_regex") else "word")
    console.print(table)


# --- natural-language rules (LLM) ---


@rules_app.command("add")
def rules_add(
    rule: str = typer.Argument(..., help='Plain English, e.g. "CCW holsters for P365 under $150"'),
    config: str = typer.Option(None, "--config", "-c"),
):
    cfg = load_config(config)
    load_env()
    if not cfg.llm.enabled or not secret("LLM_API_KEY"):
        console.print("[yellow]note: LLM_API_KEY not set — rule saved but matching needs it[/yellow]")
    state = _state(cfg)
    if state.add_rule(rule):
        state.save()
        console.print(f"[green]added rule[/green] {rule!r}")
    else:
        console.print(f"[yellow]already exists[/yellow] {rule!r}")


@rules_app.command("remove")
def rules_remove(
    rule: str = typer.Argument(...),
    config: str = typer.Option(None, "--config", "-c"),
):
    cfg = load_config(config)
    state = _state(cfg)
    if state.remove_rule(rule):
        state.save()
        console.print("[green]removed[/green]")
    else:
        console.print("[yellow]not found[/yellow]")


@rules_app.command("list")
def rules_list(config: str = typer.Option(None, "--config", "-c")):
    cfg = load_config(config)
    state = StateStore(cfg.state_path)
    rules = state.rules()
    if not rules:
        console.print('[dim]no rules — add some with `armory rules add "..."`[/dim]')
        return
    for r in rules:
        console.print(f"• {r}")


@app.command()
def doctor(config: str = typer.Option(None, "--config", "-c")):
    """Check each source's health and alert-channel configuration."""
    load_env()
    cfg = load_config(config)
    table = Table(header_style="bold")
    table.add_column("component")
    table.add_column("status")
    table.add_column("detail", overflow="fold")
    adapters = build_adapters(cfg)
    for name, scfg in cfg.sources.items():
        if not scfg.enabled:
            table.add_row(f"source:{name}", "[dim]disabled[/dim]", "")
            continue
        if name not in adapters:
            table.add_row(f"source:{name}", "[red]not built[/red]", "credential missing? see `armory setup`")
            continue
        health = adapters[name].health()
        status = "[green]ok[/green]" if health.ok else "[red]fail[/red]"
        table.add_row(f"source:{name}", status, health.detail)
    if cfg.llm.enabled:
        from .llm import LLMClient

        llm = LLMClient()
        if llm.configured:
            detail = llm.ping()
            status = "[green]ok[/green]" if detail.startswith("key OK") else "[red]fail[/red]"
            table.add_row("llm", status, detail)
        else:
            table.add_row("llm", "[yellow]missing[/yellow]", "set LLM_API_KEY in .env (rules/extraction off until then)")
    else:
        table.add_row("llm", "[dim]disabled[/dim]", "")
    for chan in ("discord", "imessage"):
        enabled = getattr(cfg.alerts, chan).enabled
        var = "DISCORD_WEBHOOK_URL" if chan == "discord" else "IMESSAGE_TO"
        if not enabled:
            table.add_row(f"alert:{chan}", "[dim]disabled[/dim]", "")
        elif secret(var):
            table.add_row(f"alert:{chan}", "[green]configured[/green]", f"{var} set")
        else:
            table.add_row(f"alert:{chan}", "[yellow]missing[/yellow]", f"set {var} in .env")
    console.print(table)


@app.command()
def setup():
    """Print credential setup instructions for the authed sources."""
    load_env()
    console.print(
        """
[bold]armory setup[/bold]

[bold]1. Discord webhook[/bold] (for alerts)
   Discord server → Server Settings → Integrations → Webhooks → New Webhook → Copy URL
   → put it in .env as DISCORD_WEBHOOK_URL=...

[bold]2. gafshub.com[/bold] (login-walled)
   a. Register at https://gafshub.com (21+ ToS checkbox; may require Reddit verification)
   b. Stay logged in, open browser DevTools → Application → Cookies → gafshub.com
   c. Copy the value of the [bold]_t[/bold] cookie
   d. .env:  GAFSHUB_COOKIE="_t=PASTE_VALUE_HERE"

[bold]3. caguns.net[/bold] (Cloudflare + login-walled classifieds)
   a. Register at https://caguns.net and log in; click through the 18+ gate
   b. DevTools → Network → click any caguns.net request → Request Headers → Cookie
   c. Copy the whole Cookie header string (xf_session=...; xf_user=...; others)
   d. .env:  CAGUNS_COOKIES="PASTE_WHOLE_HEADER"

[bold]4. iMessage alerts[/bold] (optional)
   .env:  IMESSAGE_TO="your-friend@example.com" (email/phone that receives iMessages)
   First send will prompt for Automation permission — click Allow.

[bold]5. LLM rules / extraction / scam scoring[/bold] (optional, any OpenAI-compatible API)
   .env:  LLM_API_KEY=...  (plus LLM_API_BASE / LLM_MODEL if not OpenAI)
   Then:  armory rules add "CCW holsters for P365 under $150"  &&  armory test-llm

Then run: [bold]armory doctor[/bold] to verify, [bold]armory watch[/bold] to start.
First run of each source seeds its state silently; alerts start from the second cycle.
"""
    )


@app.command()
def test_alerts(config: str = typer.Option(None, "--config", "-c")):
    """Send a test alert through every enabled channel."""
    cfg, _, alerters, _ = _boot(config)
    enabled = alerters.enabled()
    if not enabled:
        console.print("[yellow]no alert channels enabled+configured (check config.yaml + .env)[/yellow]")
        raise typer.Exit(1)
    sample = Listing(
        source="armory",
        external_id="test",
        url="https://example.com/",
        title="Test listing — armory alerts are working",
        price="$0",
        body="If you can read this, alerts are configured correctly.",
        matched_keywords=["test"],
    )
    errors = alerters.send_all([sample])
    if errors:
        for e in errors:
            console.print(f"[red]{e}[/red]")
        raise typer.Exit(1)
    console.print(f"[green]test alert sent via:[/green] {', '.join(enabled)}")


@app.command()
def test_llm(config: str = typer.Option(None, "--config", "-c")):
    """Run two sample listings through LLM extraction + rules to verify the key."""
    from .llm import LLMError, apply_result

    cfg, _, _, llm = _boot(config)
    if llm is None:
        console.print("[yellow]llm disabled in config.yaml[/yellow]")
        raise typer.Exit(1)
    if not llm.configured:
        console.print("[red]LLM_API_KEY not set in .env[/red]")
        raise typer.Exit(1)
    samples = [
        Listing(
            source="test", external_id="s1", url="https://example.com/1",
            title="WTS Glock 19 Gen 5 with Holosun 507C $750 shipped OBO",
            body="Like new, ~500 rounds. Includes box and 3 mags. PayPal FF or Zelle.",
            price="$750",
        ),
        Listing(
            source="test", external_id="s2", url="https://example.com/2",
            title="Brand NEW Trijicon ACOG TA31 - $250!! First come first served DM me",
            body="Selling for my cousin, no pics yet, cash app only, shipped from overseas.",
            price="$250",
        ),
    ]
    rules = ["compact red dots under $400", "anything Glock 19 related"]
    try:
        results = llm.classify_batch(samples, rules)
    except LLMError as exc:
        console.print(f"[red]LLM call failed: {exc}[/red]")
        raise typer.Exit(1)
    for listing in samples:
        apply_result(listing, results.get(listing.external_id, {}))
        console.print(
            f"[bold]{listing.title[:60]}[/bold]\n"
            f"  brand={listing.brand} model={listing.model} type={listing.item_type} "
            f"price=${listing.price_usd} condition={listing.condition} scam={listing.scam_risk}\n"
            f"  rules={listing.matched_rules}"
        )


@app.command()
def version():
    console.print(f"armory {__version__}")


def main():
    app()


if __name__ == "__main__":
    main()
