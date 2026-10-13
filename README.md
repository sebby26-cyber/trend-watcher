<div align="center">

# trend-watch

**An always-on research watcher that surfaces fresh, rising signals from public sources and leaves you a digest to review. It never posts or trades.**

[![Python](https://img.shields.io/badge/python-3.9%2B-blue?logo=python&logoColor=white)](https://www.python.org/)
[![License: GPL v3](https://img.shields.io/badge/license-GPL--3.0-blue)](LICENSE)
[![Tests](https://img.shields.io/badge/tests-pytest-brightgreen)](#running-tests)
[![Dependencies](https://img.shields.io/badge/dependencies-none%20(stdlib%20only)-lightgrey)](#quick-start)

</div>

---

## What it does

`trend-watch` polls public feeds (Hacker News, Reddit, GitHub Trending, Google News,
arXiv, Product Hunt, YouTube, Polymarket, Kalshi, CoinGecko, Google Trends and more)
on a schedule. It stores every observation in a local SQLite database and scores items
locally on rank, velocity, cross-platform co-occurrence and freshness. Only the best
candidates go to the [TypeSafe](https://typesafe.ai) **Jev** model, which answers
typed, closed-set questions about them (domain, lane, reason, strength, direction)
under a strict daily budget. The result is a daily Markdown digest, optional template
drafts for you to review, and a JSONL research feed.

Optional **alert packs** read the same local state, cluster fresh stories and write
a short Markdown nudge when a story clears a strict bar. You supply packs yourself;
the repository ships one fictional example.

A separate **shadow intelligence engine** ingests the SEC EDGAR 8-K Atom feed. It
matches filings against your watchlist by CIK and applies deterministic 8-K item rules
that classify each event as `REVIEW`, `IGNORE` or `INCOMPLETE`.

> [!IMPORTANT]
> trend-watch is a research tool. It **never** posts, places orders or messages
> anyone on its own. Every draft is marked `DRAFT — NOT POSTED — needs owner approval`,
> and nothing connects the research feed to execution. Alert packs write nudges to a
> local outbox; they run a command only when you set `nudge.command` in your own pack.

## Features

- **Zero dependencies.** Python 3.9+ standard library only. There is no `pip install` and no build step.
- **Token-frugal by design.** Fetching and scoring are free. Paid Jev calls are gated by per-day call and token budgets and can be switched off with one command.
- **Prompt-injection aware.** Item text goes only into the model's `state`, never into its instructions. Every answer is a typed Choice or Score, so the model writes no free-form prose.
- **Plug-and-play domains.** One small JSON file per topic sets keywords, exclusions, sources, lanes, tickers, thresholds and drafting. You add or retire topics without touching code.
- **Drop-in adapters.** Add a new source with about ten lines of Python in your config directory.
- **Resilient.** One failing source or bad domain file never fails a run. Sources get exponential retries and growing cooldowns, and a state lock prevents overlapping runs.
- **Scheduler included.** `setup` installs a macOS LaunchAgent or a Linux crontab line that runs twice an hour. It also gives you `enable`, `disable` and `status`.
- **No deletes.** Retiring a domain moves its file into `domains/archive/`. Queued outputs move to `<state>/outbox-delivered/`.
- **Bring-your-own alert packs.** `trend-watch pack <name> run` evaluates a pack you keep outside the repo and leaves nudge files for review. No packs are required.
- **SEC EDGAR 8-K shadow engine.** It keeps immutable observations, versions amendments and records an audit trail with acknowledgements. A readiness gate and an offline fixture replay come with it.

## How it works

```mermaid
flowchart LR
    subgraph Sources["Sources (free, public)"]
        A1[Hacker News / Reddit /<br/>GitHub Trending / arXiv]
        A2[Google News / Trends /<br/>Product Hunt / YouTube]
        A3[Polymarket / Kalshi /<br/>CoinGecko]
        A4[Drop-in adapters]
    end

    subgraph Local["Local, zero tokens"]
        S[(SQLite state)]
        SC[Score: rank pct, velocity,<br/>co-occurrence, freshness]
    end

    subgraph Jev["TypeSafe Jev (budgeted)"]
        C[Closed-set classification]
        D[Hook and tone selection]
    end

    subgraph Out["Outputs (for human review)"]
        O1[digests/YYYY-MM-DD.md]
        O2[drafts-for-approval/YYYY-MM-DD.md]
        O3[feeds/trading/YYYY-MM-DD.jsonl]
    end

    A1 & A2 & A3 & A4 --> S --> SC --> C --> O1
    C --> O3
    C --> D --> O2
    S --> P["Alert packs (optional):<br/>cluster, score, gate"] --> N["Nudges in the pack outbox"]

    E[SEC EDGAR 8-K Atom feed] --> R[Resolve and match watchlist by CIK] --> I[investment@shadow rules] --> SR[Shadow report and records]
```

1. **Fetch.** Adapters fetch public JSON or RSS with a configured User-Agent, a timeout and up to two exponential retries on 429 and 5xx responses.
2. **State and score.** URLs and titles are normalized and de-duplicated. Each score combines the platform rank percentile, positive velocity, cross-platform co-occurrence and freshness. Old items decay, and a hard age limit always excludes stale ones.
3. **Classify.** Up to `max_candidates` items go into a single Jev request. Answers below `llm.min_confidence` are flagged with `?`.
4. **Draft.** A second Jev request picks hooks and tones. Code then fills editable templates for X, Threads, LinkedIn and blog posts.

## Quick start

**Requirements:** Python 3.9 or newer on macOS or Linux. A [TypeSafe](https://typesafe.ai) API key is optional; without one, runs are unclassified.

```sh
# 1. Clone
git clone https://github.com/sebby26-cyber/trend-watcher.git
cd trend-watcher

# 2. Optional: choose where settings, data and outputs live.
#    Without these, config goes to ~/.config/trend-watch and data to
#    ~/.local/share/trend-watch (see Configuration).
export TW_CONFIG_DIR="$HOME/.config/trend-watch"
export TW_STATE_DIR="$HOME/.local/share/trend-watch"
export TW_OUT_DIR="$HOME/trend-watch-output"

# 3. Install the `trend-watch` command and the twice-hourly schedule
bin/trend-watch setup
```

`setup` copies the seed config and domains into your config directory. It symlinks a
`trend-watch` command into `/opt/homebrew/bin` or `/usr/local/bin` when either is on
your `PATH` and writable, and into `~/.local/bin` otherwise. It also writes the
scheduler entry, which runs at :07 and :37 every hour, with your resolved directories
pinned in it (`TREND_WATCH_PACKS_DIR` and `XDG_CONFIG_HOME` are forwarded too when
set). On macOS that entry is `~/Library/LaunchAgents/io.github.trend-watch.plist`
(set `TREND_WATCH_LABEL` to use another label); on
Linux it is a user crontab line ending in `# trend-watch-managed`. You can run `setup`
again at any time. To install everything without turning the schedule on, use
`setup --no-enable`.

**Set your API key (optional).** Use the environment variable:

```sh
export TYPESAFE_API_KEY="your-key-here"
```

On macOS you can use the Keychain instead:

```sh
security add-generic-password -s typesafe-api-key -a "$USER" -w "your-key-here"
```

If no key is found, every run behaves as `--no-llm` and the report says so.

**First run.** Try a dry run, which fetches and scores but writes no outputs and makes no Jev calls:

```sh
trend-watch run --domain ai-tech --dry-run
```

Then do a real run and check its status:

```sh
trend-watch run
trend-watch status
```

> [!TIP]
> Not using the scheduler? Every command also works straight from the checkout as
> `bin/trend-watch …` or `python3 bin/trend-watch …`.

## Usage

### Everyday commands

| Command | What it does |
|---|---|
| `trend-watch status` | Shows whether the schedule and Jev are on, the last run, today's Jev budget and where outputs are |
| `trend-watch run` | Runs one tick now |
| `trend-watch jev off` / `jev on` / `jev` | Stops, resumes or shows paid Jev calls; everything else keeps running |
| `trend-watch disable` / `enable` | Stops or restarts the scheduled job |
| `trend-watch budget` | Shows today's Jev calls and tokens against the limits |
| `trend-watch flush` | Moves outputs that were queued in the outbox into the outputs folder |
| `trend-watch init` | Creates any missing config from the seeds; it never overwrites your edits |
| `trend-watch pack list` | Lists the alert packs found on the pack search path |

### Run options

```sh
trend-watch run --domain ai-tech --dry-run      # fetch and score one domain, print a report, write nothing
trend-watch run --dry-run --verbose             # also dump every scored item as JSONL
trend-watch run --no-llm                        # unclassified digest, no Jev calls
trend-watch run --no-drafts                     # classify but skip drafting
trend-watch run --max-candidates 5              # cap the candidates sent to Jev
```

A dry run still records fetch and score history in SQLite, so later runs can measure
velocity. It writes no digest, drafts, feed or outbox, and it never calls Jev. Source
rate limits still apply, so a source fetched moments ago may be skipped.

A dry run prints its report on stdout; a normal run prints the same report on stderr.
Example dry-run report (numbers are illustrative):

```text
source hackernews: ok | items=60 | errors=0
source reddit: ok | items=75 | errors=0
source github_trending: ok | items=25 | errors=0

Candidate counts by domain x platform:
domain | github_trending | hackernews | reddit
ai-tech | 2 | 4 | 3

Top candidates:
rank | score | domain | platforms | age h | title
1 | 0.812 | ai-tech | hackernews,reddit | 1.4 | Open-weights model tops coding benchmark
2 | 0.744 | ai-tech | github_trending | 3.0 | New agent framework passes 10k stars
unclassified (no-llm: dry-run)
trend-watch: 160 fetched, 3/3 sources ok, 9 candidates, jev=0 (0 tok), drafts=0
```

Every run ends with a single `trend-watch: …` summary line on stdout. `status` reads the
last result back:

```text
Scheduler: on (runs at :07 and :37 every hour; mode: direct)
Jev: on
Last run: Tue 13 Oct 2026, 14:37 - ok - trend-watch: 412 fetched, 11/12 sources ok, 7 candidates, jev=1 (5210 tok), drafts=2
Today's Jev budget: 2026-10-13: calls 3/30, tokens 15630/400000, draft_calls 1 (included in calls)
Outputs: /home/you/trend-watch-output
```

### Add or retire a domain

```sh
trend-watch domains list                 # active and archived domains
trend-watch domains add my-topic         # creates <config>/domains/my-topic.json
trend-watch domains validate             # exits 1 if any domain or adapter is invalid
trend-watch domains archive my-topic     # moves the file to domains/archive/
trend-watch sources list                 # adapters, their origin, rate floor and last status
```

A domain is one JSON file. Every key is optional and inherits from `config.json`.
Nested maps merge with the defaults, and lists replace them:

```json
{
  "name": "my-topic",
  "enabled": true,
  "keywords": ["open source", "llm"],
  "exclude": ["hiring"],
  "sources": {
    "hackernews": {},
    "reddit": {"subreddits": ["LocalLLaMA"]},
    "google_news": {"queries": ["open source AI"]},
    "github_trending": {"topics": ["llm"]},
    "arxiv": {"categories": ["cs.CL"]}
  },
  "lanes": ["content"],
  "tickers": ["NVDA"],
  "thresholds": {"candidate_score": 0.55},
  "drafts": {"platforms": ["x", "linkedin", "blog"], "voice": "plain-spoken operator, no hype"}
}
```

Lanes are `content`, `trading` or both. Unclassified candidates go into the JSONL
research feed only for domains that have a `trading` lane. The seed set includes
`ai-tech`, `business-startups`, `crypto`, `culture-viral` and `markets-trading`.

These are the fields a domain may contain. All of them are optional and inherit from
`config.json`, and any other key is rejected:

| Field | Type | Rules |
|---|---|---|
| `name` | string | A lowercase slug (`[a-z0-9][a-z0-9_-]*`), unique, not `none`. It defaults to the file name |
| `enabled` | bool | Disabled domains still appear in `domains list` |
| `keywords` | list of strings | Matched on word boundaries |
| `exclude` | list of strings | Phrases that veto a match |
| `sources` | object | Keys must be registered adapters (see `trend-watch sources list`), and values are parameter objects |
| `lanes` | list | `content` and/or `trading` |
| `tickers` | list of strings | Extra asset symbols to recognize |
| `thresholds.candidate_score` | number | Between 0 and 1 |
| `freshness` | object | `max_age_h`, `hard_max_age_h` (must be at least `max_age_h`) and `cooccurrence_h` |
| `weights` | object | `rank_pct`, `velocity_pct`, `cooccurrence` and `freshness`, with a positive sum |
| `max_candidates` | integer | Per-domain candidate cap |
| `drafts` | object | `platforms` from `x`, `threads`, `linkedin`, `blog`, plus `voice`, `tones`, `templates` and `max_per_day` |
| `_help` | any | A free-form note that validation ignores |

### Alert packs (bring your own)

Alert packs are optional and pluggable. A pack is a nudge-only alert lane that you
write for your own interests and keep outside the repository. It reads the shared
`state.db` read-only, clusters fresh stories, scores velocity, relevance, source
quality, novelty and audience fit, and writes a short Markdown nudge only when a
story clears a strict bar. It never drafts, posts or trades. No packs are required,
and none for specific people or accounts ship with the project.

A pack is `<dir>/<name>.json`, and its domains live beside it in `<dir>/domains/`.
Pack names are lowercase slugs that start with a letter; `list` is reserved.
Discovery searches these directories in order, and the first `<name>.json` found
wins for each name:

| Order | Where | Notes |
|---|---|---|
| 1 | `--packs-dir DIR` | Repeatable. Give it before the subcommand (`trend-watch --packs-dir DIR run`) or after `pack` |
| 2 | `TREND_WATCH_PACKS_DIR` | One or more directories separated by `:` (`os.pathsep`) |
| 3 | `packs_dir` in `<config>/config.json` | Same path-list format; empty by default |
| 4 | `$XDG_CONFIG_HOME/trend-watch/packs/` | Defaults to `~/.config/trend-watch/packs/` |
| 5 | `packs/` in the checkout | Gitignored, so local packs are never committed |

The shipped example is [`examples/packs/example.json`](examples/packs/example.json),
a fictional space-industry pack with one domain. Every pack setting is documented
in [`examples/packs/README.md`](examples/packs/README.md). Copy the pack together
with its `domains/` folder to get started:

```sh
PACKS="${XDG_CONFIG_HOME:-$HOME/.config}/trend-watch/packs"
mkdir -p "$PACKS"
cp examples/packs/example.json "$PACKS/my-pack.json"
cp -R examples/packs/domains "$PACKS/"
trend-watch pack list
trend-watch domains validate
```

Or try the example in place without copying anything:

```sh
trend-watch --packs-dir examples/packs pack list
trend-watch --packs-dir examples/packs run --no-llm --no-drafts
```

A pack's domains resolve from `<dir>/domains/<domain>.json` first, then from the
repository's `seed/domains/`. The domains of enabled packs join the normal
collector on `run`, while your own edits in `<config>/domains/` and archived domains
keep precedence. A file at `<config>/<name>.json` deep-merges over the pack (nested
maps merge, lists replace), so you can tune a pack without editing it.

Evaluate a pack after a collector run, since it needs the shared `state.db`:

```sh
trend-watch pack my-pack run --dry-run          # score and print the top stories; no nudge, nothing recorded
trend-watch pack my-pack run --dry-run --json   # the same evaluation as JSON
trend-watch pack my-pack run                    # live evaluation; writes a nudge if a story clears the bar
trend-watch pack my-pack run --test-nudge       # force one labelled test nudge from a real story
trend-watch pack my-pack report --hours 24      # Markdown summary of recent evaluations and nudges
trend-watch pack my-pack harvest                # read reply verdicts when tuning is enabled; otherwise a no-op
```

`report` covers the last 24 hours unless you pass `--hours`. Pack state lives in
`<state>/<name>/`: `<name>.db`, `outbox/`, `sent/`, `failed/` and day-named
`log/*.jsonl` files. With `"nudge": {"command": []}`, as in the example, nudges stay
in the pack's `outbox/`. If you set `nudge.command` to an argv list, it runs without a shell
and receives `{marker}`, `{action}` and `{file}` substitutions. The scheduler does
not run packs, so call `trend-watch pack <name> run` from your own wrapper (see
`TREND_WATCH_WRAPPER` under [Configuration](#configuration)) or cron entry after
each collector run.

### Adding a source adapter

Put trusted Python code in `<config>/adapters/local_feed.py`:

```python
NAME = "local_feed"
MIN_INTERVAL_S = 900
NEEDS_KEY = False

def fetch(params: dict, ctx) -> list[dict]:
    rows = ctx.get_json(params["url"])
    return [{"title": r["title"], "url": r["url"],
             "score": r.get("score", 0), "published": r.get("published"),
             "snippet": r.get("summary", "")} for r in rows]
```

Reference it from a domain with `"sources": {"local_feed": {"url": "https://…"}}`.
Each returned item needs a non-empty `title` and `url`. Optional fields are a numeric
`score`, a positive integer `rank`, `published` (epoch or ISO), `snippet`, an `extra`
dict and `domain_hint`. `ctx.get_json` and `ctx.get_text` apply the shared HTTP
policy, and `ctx.collect(origins, callback)` keeps the origins that succeed when
others fail.

### SEC EDGAR 8-K shadow engine

The engine is **disabled by default** and refuses to fetch live data until
`intake.sec_edgar.user_agent` contains a real contact email, as SEC fair access
requires. To enable it, first edit `<config>/config.json`:

```json
"intake": {
  "sec_edgar": {
    "enabled": true,
    "user_agent": "trend-watch/0.1.0 research you@example.com"
  }
}
```

Then add a watchlist and run the stages:

```sh
# Copy the sample into your config directory (the "Settings:" path setup printed), then edit it; CIK is decisive
cp fixtures/edgar-8k/watchlist.json "${TW_CONFIG_DIR:-${XDG_CONFIG_HOME:-$HOME/.config}/trend-watch}/watchlist.json"

trend-watch tick                 # ingest -> resolve -> annotate -> consume -> render, under one lock
trend-watch records              # list active records (read-only)
trend-watch ack <record_uid> --verdict correct --note "checked"
trend-watch replay               # offline fixture replay: no network, no live Jev
trend-watch readiness            # go-live gate (read-only)
trend-watch scan                 # digit-run tripwire over shadow outputs and feeds
```

Records and the Markdown report are written to `<state>/shadow/investment/`. No
schedule is installed for `tick`, so run it yourself or add your own cron entry.

Sample `replay` tail:

```text
matched 51/51; decision precision 1.000; REVIEW precision 1.000
exceptions: 0
tripwire hits over replay outputs: 0
REPLAY PASSED
```

### Maintenance

```sh
trend-watch migrate              # apply pending forward-only state.db migrations (backs up first)
trend-watch backup [--dest DIR]  # online backup of state.db (default <state>/backups)
trend-watch restore <file>       # archive the current state.db, then restore a backup
```

## Configuration

Settings are JSON and are re-read on every run. Override only what you need in
`<config>/config.json`; [`seed/config.json`](seed/config.json) holds every default.
Unknown keys are rejected.

### Directories and environment

| Option | Env var | Default | Description |
|---|---|---|---|
| Config directory | `TW_CONFIG_DIR` | `~/.config/trend-watch` (`$XDG_CONFIG_HOME/trend-watch`) | `config.json`, `domains/`, `domains/archive/`, `adapters/`, `watchlist.json`, pack overrides `<name>.json` |
| State directory | `TW_STATE_DIR` | `~/.local/share/trend-watch` (`$XDG_DATA_HOME/trend-watch`) | `state.db`, `last-run.json`, `logs/`, `outbox/`, `feeds/trading/`, `shadow/`, `backups/`, one folder per pack |
| Output directory | `TW_OUT_DIR` | `<state>/out` | `digests/` and `drafts-for-approval/` Markdown files |
| Single home | `TREND_WATCH_HOME` | unset | One root for everything: `<home>/config`, `<home>` as the state directory, and `<home>/out` |
| Pack search path | `TREND_WATCH_PACKS_DIR` | unset | Extra pack directories, separated by `:`; see [Alert packs](#alert-packs-bring-your-own) for the full order |
| Scheduler label | `TREND_WATCH_LABEL` | `io.github.trend-watch` | launchd label and plist name used by `setup`, `enable`, `disable` and `status` |
| Scheduler wrapper | `TREND_WATCH_WRAPPER` | unset (optional) | Executable the schedule runs instead of `bin/trend-watch run`; `status` then reports `mode: custom wrapper` |
| API key | `TYPESAFE_API_KEY` | none | TypeSafe API key; if unset, the Keychain is checked next |

Each directory is resolved on its own, in this order:

- **Config:** `TW_CONFIG_DIR`, then `$TREND_WATCH_HOME/config`, then `$XDG_CONFIG_HOME/trend-watch`, then `~/.config/trend-watch`.
- **State:** `TW_STATE_DIR`, then `$TREND_WATCH_HOME`, then `$XDG_DATA_HOME/trend-watch`, then `~/.local/share/trend-watch`.
- **Output:** `TW_OUT_DIR`, then `$TREND_WATCH_HOME/out`, then `out/` inside the resolved state directory.

So with `TREND_WATCH_HOME` set, a `TW_*` variable still wins for its own directory,
and output stays at `$TREND_WATCH_HOME/out` even if `TW_STATE_DIR` points elsewhere.
`setup` prints the directories it resolved and pins them in the scheduler entry.

### Settings

| Setting | Default | Description |
|---|---|---|
| `packs_dir` | `""` | Extra pack directories (same `:`-separated format), searched after `TREND_WATCH_PACKS_DIR` |
| `llm.keychain_service` | `typesafe-api-key` | macOS Keychain service the key is read from |
| `llm.enabled` | `true` | Master switch for Jev calls (`trend-watch jev on/off` changes it) |
| `llm.model` | `jev-1.13.0` | Pinned Jev model |
| `llm.endpoint` | `https://api.typesafe.ai/v1/systemone` | TypeSafe endpoint |
| `llm.min_confidence` | `0.5` | Answers below this are shown with `?` |
| `budget.max_calls_per_day` | `30` | Daily Jev call cap (UTC day), including drafts and retries |
| `budget.max_input_tokens_per_day` | `400000` | Daily input-token cap |
| `max_candidates` | `15` | Items sent per classification request |
| `thresholds.candidate_score` | `0.55` | Minimum score (0 to 1) for an item to become a candidate |
| `freshness.max_age_h` / `hard_max_age_h` | `6` / `48` | Freshness window and hard age cutoff, in hours |
| `weights.*` | `0.35 / 0.30 / 0.15 / 0.20` | Weights for rank, velocity, co-occurrence and freshness (normalized) |
| `drafts.max_per_day` | `3` | Daily draft cap |
| `drafts.platforms` | `["x", "linkedin", "blog"]` | Any of `x`, `threads`, `linkedin`, `blog` |
| `http.user_agent` | generic string | User-Agent sent to sources; set your own |
| `http.timeout_s` / `retries` | `15` / `2` | Request timeout and retry count |
| `intake.sec_edgar.enabled` | `false` | Turns on the EDGAR 8-K intake |
| `intake.sec_edgar.user_agent` | placeholder | Must include your contact email, for example `you@example.com` |
| `intake.sec_edgar.request_interval_s` | `0.2` | Seconds between SEC requests; the minimum allowed is `0.1` (10 requests per second) |
| `investment.watchlist_path` | `watchlist.json` | Watchlist path, relative to the config directory |
| `retention.days` | `30` | Retention for state tables |
| `hooks.notify_enabled` / `publish_enabled` | `false` | Stub hooks only. `publish` is never called |

> [!NOTE]
> UTC defines output file names and budget days; the schedule uses local wall-clock
> time. On macOS, if privacy protection blocks a write to the output directory, the
> file is queued in `<state>/outbox/`. `trend-watch flush` delivers it later.

## Project structure

```text
trend-watcher/
├── bin/
│   └── trend-watch              # CLI launcher (no install needed)
├── trend_watch/
│   ├── cli.py                   # argparse entry point and subcommands
│   ├── config.py                # path resolution, config merge and validation, domains
│   ├── setup.py                 # setup, launchd/cron scheduler, status
│   ├── pipeline.py              # fetch -> score -> classify -> draft -> deliver
│   ├── packs.py                 # optional nudge-only alert packs (discovery, scoring, nudges)
│   ├── registry.py              # adapter discovery (built-in and drop-in)
│   ├── http.py                  # shared HTTP policy, retries, cooldowns
│   ├── scoring.py               # normalization, velocity, composite score
│   ├── jev.py                   # TypeSafe Jev client, budgets, typed parsing
│   ├── drafts.py                # template drafter
│   ├── outputs.py               # digests, outbox and flush
│   ├── state.py, migrations.py, backup.py, lock.py
│   ├── engine.py                # shadow engine stages (tick/ingest/resolve/…)
│   ├── intake/sec_edgar.py      # SEC EDGAR 8-K Atom intake
│   ├── consumers/investment.py  # deterministic 8-K item rules
│   ├── observations.py, annotator.py, watchlist.py, ids.py
│   ├── replay.py, readiness.py, tripwire.py, records.py
│   ├── safexml.py, serialize.py
│   ├── adapters/                # hackernews, reddit, arxiv, github_trending, …
│   └── hooks/                   # notify / publish / external drafter stubs
├── seed/
│   ├── config.json              # every default setting
│   └── domains/*.json           # starter domains copied by init/setup
├── examples/packs/
│   ├── README.md                # full alert pack schema
│   ├── example.json             # fictional starter pack to copy
│   └── domains/example-launches.json
├── fixtures/edgar-8k/           # offline 8-K fixtures and a sample watchlist
├── tests/                       # pytest suite (conftest.py isolates pack discovery)
├── docs/                        # DESIGN-SPEC.md and R1-SPEC-DELTAS.md
├── packs/                       # optional local packs (gitignored, not in the repo)
└── LICENSE                      # GNU GPL v3.0
```

## Running tests

```sh
python3 -m pytest -q
```

The suite needs no secrets or network access. It runs in temporary directories with
fake adapters, fixture RSS and JSON, and fake Jev HTTP responses, and it blocks real
HTTP. `tests/conftest.py` points pack discovery at an empty directory, so packs you
have installed never leak into a test run. pytest is the only test dependency; the
tool itself needs nothing beyond the standard library. Most tests are also plain
`unittest` cases, so `python3 -m unittest discover -s tests` runs that subset
without pytest. To check the 8-K rules end to end offline, run `trend-watch replay`.

## Roadmap

- **Shadow to production for the 8-K engine.** Production rendering stays off until `trend-watch readiness` passes: 14 shadow days, 20 spot-checks and REVIEW precision of at least 0.8.
- **Key-based adapters.** `x`, `tiktok` and `instagram` are registered stubs that still need an API key or a scraper.
- **External drafter backend.** `drafts.backend: "external"` calls a documented stub (`hooks/drafter_external.py`), and no generative backend ships yet.

## Contributing

Contributions are welcome.

1. Fork the repo and create a feature branch.
2. Keep to the house rules: standard library only, Python 3.9 compatible, no secrets in code or config, and no file deletions. Item text must stay out of model instructions.
3. Add or update tests and run `python3 -m pytest -q`.
4. Run `trend-watch domains validate` if you touched seeds or adapters.
5. Open a pull request that describes the change and its motivation.

By contributing, you agree that your contributions are licensed under the
GNU General Public License v3.0.

## Disclaimer

trend-watch is a research and monitoring tool. **Nothing it produces is financial,
investment or trading advice.** Classifications, "direction" fields and research
feeds are automated observations that can be wrong, so verify primary sources
before you act on them.

When you enable SEC EDGAR intake, follow the
[SEC's fair access policy](https://www.sec.gov/os/accessing-edgar-data): send a
User-Agent with your contact email and stay at or below 10 requests per second.
trend-watch enforces a minimum request interval and refuses to fetch with a
placeholder User-Agent. You are still responsible for how you use it. Respect the
terms of service and rate limits of every other source you configure.

## License

Released under the [GNU General Public License v3.0](LICENSE).
