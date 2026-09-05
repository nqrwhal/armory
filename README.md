# armory

Personal listing monitor & search across four firearm marketplace/community sites:

| Source | What it watches | Access |
|---|---|---|
| **calguns.net** | Marketplace forums via vBulletin RSS (handguns, long guns, parts/accessories, ammo, reloading, non-firearm, WTB, commercial) | public |
| **tacswap.com** | Category feeds (server-rendered HTML) + site-wide search via its internal Next.js server action | public |
| **gafshub.com** | Latest topics via Discourse JSON (the GAFS successor after Reddit banned r/GunAccessoriesForSale) | **login required** |
| **caguns.net** | CAS classifieds listings (XenForo HTML) — behind Cloudflare + an 18+ gate + a classifieds login wall | **login required** |

**No database.** All state lives in one small JSON file (`armory.state.json`):
per-source last-check timestamps plus a ring of recently-seen listing IDs for
dedupe, and your keywords/rules. Only genuinely new listings get enriched,
LLM-classified, and alerted. First run of each source seeds silently.

## Setup

```bash
uv venv --python 3.12          # or: python3.12 -m venv .venv
uv pip install .               # or: .venv/bin/pip install .
cp .env.example .env           # then edit (see below)
.venv/bin/armory setup         # step-by-step credential instructions
.venv/bin/armory doctor        # verify each source & channel
```

### Credentials (`.env`)

- `DISCORD_WEBHOOK_URL` — Discord server → Settings → Integrations → Webhooks → Copy URL.
- `GAFSHUB_COOKIE` — log into gafshub.com, DevTools → Application → Cookies → copy the
  `_t` cookie value as `_t=...`. (Alternative: `GAFSHUB_USER_API_KEY`.)
- `CAGUNS_COOKIES` — log into caguns.net, click through the 18+ gate, DevTools → Network →
  any request → copy the whole `Cookie` request header.
- `IMESSAGE_TO` — email/phone that receives iMessages (optional; first send prompts for
  macOS Automation permission).
- `LLM_API_KEY` / `LLM_API_BASE` / `LLM_MODEL` — any OpenAI-compatible API, including
  Anthropic-style endpoints (e.g. the GLM coding plan via
  `LLM_API_BASE=https://api.z.ai/api/anthropic`, `LLM_MODEL=glm-5-turbo`).

Both authed sites are free accounts. Cookie lifetimes are long (months) but not forever —
when they expire `armory doctor` says so and `armory setup` shows how to refresh.

## Usage

```bash
armory watch                  # poll forever (this is what the LaunchAgent runs)
armory poll [--source X]      # one-shot poll
armory search "glock 19" [--source tacswap] [--limit N]   # live site search
armory status                 # state file: last checks, keyword/rule counts
armory keywords add surefire          # case-insensitive whole-word include
armory keywords add "g\\s*19" --regex # raw regex
armory keywords add airsoft --exclude # -term: suppress listings mentioning it
armory keywords add glock --trades    # {trade}term: also match WTT/WTB listings
armory keywords add -- -airsoft       # prefix syntax works inline too (note the --)
armory rules add "CCW holsters for P365 under $150"       # natural-language (needs LLM)
armory rules list | remove "<rule text>"
armory test-llm               # verify the LLM key with two sample listings
armory doctor                 # per-source health + alert channel + LLM check
armory test-alerts            # send a test alert to enabled channels
```

### Keyword modes and trade/WTB filtering

Demand-side listings — people **WTT (trading for)** or **WTB (buying)** an item —
never alert on plain keywords: a "WTB Glock 19" post is noise when you're
watching for Glocks being sold. Intent is detected from source-native data
(caguns ad prefixes, tacswap post types), the LLM's `wants_to` extraction, or
title conventions (`WTT`, `WTB`, `ISO`, `[WTT]`, …) as a floor — and it applies
to LLM rules too.

- `term` / `+term` — include (default)
- `-term` — exclude: suppresses the listing even when include keywords match
  (e.g. `-airsoft`, `-magazine` for parts searches)
- `{trade}term` — include *and* alert on demand-side listings mentioning the
  term (you want buyers/traders when you're selling)

## How detection works

- Each source polls page 1 of its feeds at its own interval (calguns/tacswap/gafshub
  2 min, caguns 10 min) with ±20% jitter.
- A listing is "new" if its ID isn't in the source's recent-ID ring (500 IDs) and
  it isn't older than `last_check − 15 min`. New listings get at most one
  enrich fetch each (12/cycle cap) for their full description.
- Keyword matching always runs (title, description, category, location, seller).
- With an LLM key, one batched call per source per cycle (10 listings/batch) also
  extracts structured fields, matches natural-language rules, and scores scam risk.
- Alerts are capped per cycle (`alerts.max_per_cycle`, default 6); overflow is
  summarized as a "+N more suppressed" note instead of flooding the channel.
- Edits/bumps don't re-alert (same listing ID = already seen). Nothing is stored
  per listing — after the ID leaves the ring, only the timestamp cutoff applies.

## Alert etiquette

caguns.net added its classifieds login wall in June 2026 explicitly because of
scrapers — its interval stays slow (10 min) on purpose, and aggressive polling
risks the account whose cookies you're using. The other sites are polled at
human-scale frequencies with a browser-grade client.

## Run at login (macOS LaunchAgent)

```bash
mkdir -p ~/.armory
sed "s|ARMORY_HOME|$(pwd)|g;s|VENV_PY|$(pwd)/.venv/bin/python|;s|LOGPATH|$HOME/.armory/watch.log|" \
  scripts/com.armory.watch.plist.template > ~/Library/LaunchAgents/com.armory.watch.plist
launchctl load ~/Library/LaunchAgents/com.armory.watch.plist
```

Logs go to `~/.armory/watch.log`. Stop with `launchctl unload ...`.

## Run on a server (Linux, systemd user service)

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/armory.service <<EOF
[Unit]
Description=armory listing watcher
After=network-online.target

[Service]
WorkingDirectory=%h/armory
ExecStart=%h/armory/.venv/bin/python -m armory.cli watch
Restart=on-failure
RestartSec=30

[Install]
WantedBy=default.target
EOF
loginctl enable-linger $USER   # keep it running when you log out (may need sudo)
systemctl --user daemon-reload && systemctl --user enable --now armory.service
journalctl --user -u armory.service -f   # tail the logs
```

Note: iMessage alerts are macOS-only; Discord works everywhere. When gafshub's
session needs refreshing on the server: paste a fresh `GAFSHUB_COOKIE` into
`.env`, `rm gafshub.cookies.json`, and `systemctl --user restart armory` — the
cookie jar keeps Discourse's rotating `_t` token alive between restarts, so
routine re-exports are rare.

## Site quirks (for future maintenance)

- **tacswap search** replays the SPA's Next.js server action. The action id rotates
  whenever they deploy, but it ships in the RSC payload of every category page
  (`\"id\":\"<hex>\",\"bound\"`), so the adapter re-discovers it automatically on
  first search and after any rejection — the discovery GET also primes the GAESA
  edge cookie. No manual maintenance.
- **calguns** finished a server migration recently (vBulletin 6); if RSS shape changes,
  the HTML fallback path is `/forum/marketplace/.../page1`.
- **caguns** ads use XenForo `structItem--ad` blocks — the parser is fixture-tested
  (`tests/fixtures/`), so layout changes fail loudly in tests, not silently in prod.

## State persistence

Relative `state_path` values resolve against the directory containing the selected
config file, including when using `--config` from another working directory.
If you previously ran that way, move your existing state file alongside the
config or set an absolute `state_path` before restarting.

State saves use a POSIX file lock and atomic replacement on macOS/Linux. Changes
to independent top-level sections are preserved across concurrent writers;
competing edits to the same section raise `StateConflictError` instead of silently
overwriting data. Reload and retry after a conflict. The `.lock` file is persistent;
do not delete it while processes are running. Restart existing watchers after
upgrading so all writers use the locking protocol.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```
