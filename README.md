# armory

Personal listing monitor & search across four firearm marketplace/community sites:

| Source | What it watches | Access |
|---|---|---|
| **calguns.net** | Marketplace forums via vBulletin RSS (handguns, long guns, parts/accessories, ammo, reloading, non-firearm, WTB, commercial) | public |
| **tacswap.com** | Category feeds (server-rendered HTML) + site-wide search via its internal Next.js server action | public |
| **gafshub.com** | Latest topics via Discourse JSON (the GAFS successor after Reddit banned r/GunAccessoriesForSale) | **login required** |
| **caguns.net** | CAS classifieds listings (XenForo HTML) — behind Cloudflare + an 18+ gate + a classifieds login wall | **login required** |

Poll state lives in one small JSON file (`armory.state.json`): per-source
last-check timestamps plus a ring of recently-seen listing IDs for dedupe, and
your keywords/rules. Only genuinely new listings get enriched, LLM-classified,
and alerted. First run of each source seeds silently. The deal hunter adds a
SQLite DB (`armory.db`) for listings, valuations, comps, and the CA roster —
see [Deal hunter](#deal-hunter-calguns--92122-radius).

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
- `VALUATION_MODEL` (optional, default `glm-5.3-flash`) and
  `ZAI_SEARCH_MCP_URL` (optional, default z.ai's web-search MCP) — the deal
  hunter reuses `LLM_API_KEY` for both.

Both authed sites are free accounts. Cookie lifetimes are long (months) but not forever —
when they expire `armory doctor` says so and `armory setup` shows how to refresh.

## Usage

```bash
armory watch                  # poll forever (this is what the LaunchAgent runs)
armory poll [--source X]      # one-shot poll
armory search "glock 19" [--source tacswap] [--limit N]   # live site search
armory status                 # state file: last checks, keyword/rule counts, db size
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

# deal hunter (see next section)
armory roster refresh         # load the CA DOJ handgun roster into the DB
armory backfill [--forum handguns|long_guns|all] [--days 90] [--pages N] [--enrich N]
armory evaluate [--limit N] [--loop]   # run valuations over the deal queue
armory deals [--top N]        # best-scoring open listings within radius
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

## Deal hunter (calguns + caguns → 92122 radius)

On top of keyword alerts, armory continuously parses the calguns and caguns
marketplaces (handguns + long guns categories per `valuation.gun_forums`),
keeps every listing in SQLite (`armory.db`), and runs an AI valuation on
anything for sale within `watch.radius_miles` of `watch.zip` (default:
100 mi around 92122 / UTC San Diego):

```
thread/ad lists ─→ armory.db ─→ geo (zip → city/region, longest match wins;
                                 caguns Region/Sub-Region resolves structurally)
                              ─→ CA roster lookup (deterministic, fuzzy)
                              ─→ GLM-5.3-Flash valuation:
                                   thinking ON + web_search tool (z.ai MCP),
                                   local comps from the DB, attachment pricing,
                                   off-roster premium → deal score 0-100
                              ─→ Discord/iMessage alert at score ≥ 70
```

caguns CAS ads arrive with structured fields (price, Region/Sub-Region,
Caliber, and the site's own Roster tag) that flow straight into the DB and the
valuation context; calguns titles get the regex treatment with lazy body
fetches when they hide price/location. Full ad bodies are fetched lazily at
valuation time so only in-radius queue rows cost a request.

- **Backfill** (`armory backfill [--source calguns|caguns|all]`) walks the
  lists newest→oldest until everything older than `backfill.days` (90) has
  been seen, throttled per source (`backfill.intervals`; caguns runs slower
  on purpose — the site is explicitly anti-scraper) and resumable
  (Ctrl-C and re-run). It builds the historical comps the valuations lean on.
- **Live** — every `armory watch` cycle also ingests page 1 of each gun forum
  (catching new threads, price edits, and SOLD edits) and drains a couple of
  valuations (`valuation.max_per_cycle`), so new listings are valued as they
  arrive without slowing the keyword pipeline.
- **Valuation** — one GLM-5.3-Flash call per listing (`VALUATION_MODEL`
  overrides): identity + attachments itemized with values, market range from
  live web prices (GunBroker/dealers) weighted against local comps, roster
  status reconciled with the deterministic DOJ lookup (generation mismatches
  are deliberately escalated to the model rather than guessed), off-roster
  premium for handguns, and a 0-100 deal score. Score ≥
  `valuation.alert_min_score` with a good/great verdict alerts; a re-alert
  only fires when the price drops >5% after the first alert. Deal alerts are
  score-driven and independent of keywords — to hard-skip listings from the
  deal pipeline (no valuation, no alert), list substrings in
  `valuation.exclude_terms` (e.g. `rmr hd`).
- **Roster** — `armory roster refresh` fetches the DOJ certified + de-certified
  handgun tables (~3.7k entries) into the DB. Long guns skip roster logic
  (handgun-only law).
- **Law playbook** — `src/armory/data/ca_transfer_laws.md` (researched, dated,
  sourced) is appended to every valuation prompt while
  `valuation.law_playbook` is on: exact PPT/DROS fees ($47.19 + $10/extra
  gun), the 3-per-30-days purchase cap (AB 1078, post-*Rhodes*), C&R/FFL03+COE
  rules, the >10-round magazine ban, waiting period, interstate restrictions,
  and how each should move the deal score. Update it when laws change — the
  model treats it as authoritative over its own memory.
- **Web search** — the model's `web_search` tool runs through z.ai's web-search
  MCP server (`https://api.z.ai/api/mcp/web_search_prime/mcp`, same
  `LLM_API_KEY`, Bearer auth). If it's unreachable the valuation degrades to
  local comps + roster with no tool calls — nothing crashes.
- **Disk** — bodies are capped at ~8 KB and rows are ~1 KB; growth is a few
  MB/month. `db.retention_days` (default 0 = keep everything) prunes stale
  rows while archiving their price history into `comps_archive` first, so old
  data keeps feeding comps after pruning.
- Geo resolution is honest about fuzziness: zips and cities resolve exactly;
  "SoCal"/"NorCal"-style region tags resolve to a centroid and are tagged as
  approximate (longest match wins, so "Inland Empire" is the region, not the
  town of Empire); listings with no resolvable location are stored but never
  valued or alerted on.

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
  the HTML fallback path is `/forum/marketplace/.../page1`. The deal hunter scrapes
  those thread-list pages directly (`table.topic-list-container`, stickies skipped,
  50 threads/page, sorted by last activity); the canonical forum URL is re-derived
  from the RSS `<category domain>` each session with verified slugs as fallback, so
  a slug restructure self-heals. List and thread parsers are fixture-tested.
- **caguns** ads use XenForo `structItem--ad` blocks — the parser is fixture-tested
  (`tests/fixtures/`), so layout changes fail loudly in tests, not silently in prod.
- **CA DOJ roster pages** (certified + de-certified) are single server-rendered
  HTML tables with no pagination — if DOJ ever paginates or restructures,
  `armory roster refresh` errors loudly and the valuation falls back to
  `unknown` roster status (model-adjudicated via web search).

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

The deal-hunter DB (`armory.db`) runs in WAL mode with a 30 s busy timeout, so
`armory watch`, an in-flight `backfill`, and CLI commands can all touch it at
once; a `backfill` can be Ctrl-C'd at any point and re-run — per-page progress
and per-listing rows are committed as they go.

## Tests

```bash
PYTHONPATH=src .venv/bin/python -m pytest -q
```
