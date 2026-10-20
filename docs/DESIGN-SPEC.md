# trend-watch — build spec

Build a token-frugal, always-on "trend-watch" looper. Everything in this repo is
built here, then installed by a separate step (not yours) into these final homes:

| What | Installed home | Env override |
|---|---|---|
| code (python package + CLI) | the repository checkout | — |
| owner-editable config | `$XDG_CONFIG_HOME/trend-watch/` (default `~/.config/trend-watch/`; `config.json`, `domains/`, `adapters/`) | `TW_CONFIG_DIR` |
| machine state | `$XDG_DATA_HOME/trend-watch/` (default `~/.local/share/trend-watch/`; `state.db`, `outbox/`, `feeds/`, `logs/`) | `TW_STATE_DIR` |
| owner-facing outputs | `<state>/out/` (`digests/`, `drafts-for-approval/`) | `TW_OUT_DIR` |

Resolution order per root: explicit `TW_*_DIR`, then `TREND_WATCH_HOME`
(config is its `config/` child, state is the root, output is its `out/` child),
then XDG defaults. Avoid owner-specific absolute paths.

API contract: Jev requests use `questions` as a map keyed by ID, answers are
keyed by the same ID, and usage reports `usage.input_tokens`. The scheduler
uses StartCalendarInterval rather than StartInterval on macOS.

## Hard constraints

- **Python 3.9 stdlib only** (urllib.request, sqlite3, json, xml.etree, hashlib, argparse, importlib, concurrent.futures). No httpx, no PyYAML, no pip installs. Config is JSON.
- Launcher is bash 3.2 compatible (`set -u`, never `set -e`), BSD userland.
- No secrets in code or config. The TypeSafe key is read from env `TYPESAFE_API_KEY`, else macOS keychain via `security find-generic-password -s <service> -w` where `<service>` comes from config (default `typesafe-api-key`). If neither exists, the run behaves as `--no-llm` and says so in the report.
- Nothing is ever posted, sent, or notified. Leave stub hook points only (see Hooks).
- No deletes anywhere: retiring a domain = moving its file to `domains/archive/`.
- One source failing must never fail the run. One bad domain file must never crash the run (skip it, record the error).
- Item text is untrusted data: it only goes into Jev `state`, never into instructions.

## Plug-and-play (owner requirement — no code edits for normal changes)

1. **Global config** `config.json`: defaults for everything (thresholds, weights, freshness, budgets, LLM settings, draft settings, per-adapter defaults like min interval/timeouts/user agent). Hot-reload: read fresh every run.
2. **One domain = one small JSON file** in `domains/`. Drop a file in to add; move to `domains/archive/` to retire. Every key is optional and inherits from global defaults:
   ```json
   {
     "name": "ai-tech",
     "enabled": true,
     "keywords": ["openai", "llm", "gpt", "anthropic", "nvidia"],
     "exclude": ["hiring", "who is hiring"],
     "sources": {
       "hackernews": {},
       "reddit": {"subreddits": ["MachineLearning", "LocalLLaMA", "singularity"]},
       "google_news": {"queries": ["AI model launch"]},
       "github_trending": {"topics": ["llm", "ai-agents"]},
       "arxiv": {"categories": ["cs.AI", "cs.CL"]}
     },
     "lanes": ["content"],
     "tickers": ["NVDA", "MSFT"],
     "thresholds": {"candidate_score": 0.55},
     "drafts": {"platforms": ["x", "linkedin", "blog"], "voice": "plain-spoken operator, no hype"}
   }
   ```
3. **Adapters registered by name.** Built-ins live in the package `adapters/` dir; drop-in adapters live in `$TW_CONFIG_DIR/adapters/*.py` and are discovered at run start. Tiny interface (document it in README with a ≤15-line example):
   ```python
   NAME = "hackernews"            # registry key used in domain files
   MIN_INTERVAL_S = 900           # optional; per-source rate floor
   NEEDS_KEY = False              # optional; stubs set a reason string instead
   def fetch(params: dict, ctx) -> list[dict]:
       # params = merged params from every enabled domain that references this adapter
       # ctx.get_json(url, headers=None) / ctx.get_text(url) — framework HTTP with UA, timeout, retry/backoff
       # return items: {"title", "url", "score"(number, platform-native), "rank"(int, optional),
       #               "published"(ISO or epoch, optional), "snippet"(optional), "extra"(dict, optional),
       #               "domain_hint"(optional domain name when params were domain-specific, e.g. a subreddit)}
   ```
   The framework merges list params across domains (union) and calls each adapter once per run, records `domain_hint` per origin, and enforces: per-source min interval (skip with "rate-floor" status if fetched too recently), timeout, retry with exponential backoff on 429/5xx (max 2 retries), and a per-source consecutive-failure backoff (after N failures, skip for an increasing cooldown, stored in state.db).
4. **Validation at run start**: each domain file is validated (JSON parses, known keys, types, referenced adapters exist). A bad file is skipped with an error line in the run report and digest footer; the run continues.
5. **CLI** `bin/trend-watch` (python entry, `#!/usr/bin/env python3`, works without install):
   - `trend-watch domains list` — name, enabled, adapters, lanes, file path; archived ones listed separately.
   - `trend-watch domains add <name>` — writes a commented-by-convention starter JSON (use a `"_help"` key with guidance strings, ignored by the validator) into `domains/`; refuses to overwrite.
   - `trend-watch domains validate` — validate all, exit 1 if any invalid.
   - `trend-watch domains archive <name>` — move file into `domains/archive/` (never delete).
   - `trend-watch sources list` — every registered adapter, built-in vs drop-in, stub/needs-key status, min interval, last fetch status from state.db.
   - `trend-watch run [--domain X] [--no-llm] [--no-drafts] [--dry-run] [--max-candidates K]` — one tick. `--domain` restricts to one domain (for testing a new one in isolation). `--dry-run` = fetch + score + print, write nothing to outputs (state db still records fetch/score history so a second dry run sees velocity — document this), and never calls Jev.
   - `trend-watch flush` — move anything in `outbox/` (outputs that could not be written to `TW_OUT_DIR`) into place.
   - `trend-watch init` — create config dir with `config.json` + seed domain files if missing (never overwrite).
   - `trend-watch budget` — today's Jev calls/tokens vs limits.
   - Every `run` prints a final one-line summary: `trend-watch: <N> fetched, <S>/<T> sources ok, <C> candidates, jev=<calls> (<tokens> tok), drafts=<D>`.

## Pipeline

### Layer 1 — fetch (zero tokens)
Built-in adapters (all free, no key; real User-Agent from config, e.g. `trend-watch/0.1.0 (+https://github.com/sebby26-cyber/trend-watcher)`):
- `hackernews` — `https://hacker-news.firebaseio.com/v0/topstories.json` + item fetch for top N (default 60, concurrent with a small thread pool), or Algolia `https://hn.algolia.com/api/v1/search?tags=front_page`. Prefer Algolia (one request). score=points, extra.comments.
- `reddit` — `https://www.reddit.com/r/<sub>/hot.json?limit=25` and `rising.json`; score=ups, published=created_utc, domain_hint per sub owner.
- `google_trends` — daily trends RSS `https://trends.google.com/trending/rss?geo=<GEO>` (geo from params, default US). Parse `ht:approx_traffic` into score.
- `github_trending` — GitHub search API `https://api.github.com/search/repositories?q=created:>YYYY-MM-DD+topic:<t>&sort=stars&order=desc&per_page=20` (no token; respect 10 req/min); also a no-topic query. score=stargazers.
- `producthunt` — RSS `https://www.producthunt.com/feed`.
- `youtube` — channel/playlist RSS `https://www.youtube.com/feeds/videos.xml?channel_id=<id>` from params (no free trending feed exists; say so in README). With no channel ids configured, it returns [] with status "not-configured".
- `google_news` — `https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en` per query.
- `arxiv` — `http://export.arxiv.org/rss/<category>` per category.
- `polymarket` — `https://gamma-api.polymarket.com/markets?active=true&closed=false&order=volume24hr&ascending=false&limit=50`; score=volume24hr, extra has price/outcomePrices and 1-day price change if present.
- `kalshi` — `https://api.elections.kalshi.com/trade-api/v2/markets?status=open&limit=100` sort by volume_24h in code; score=volume_24h.
- `coingecko` — `https://api.coingecko.com/api/v3/search/trending`; score from rank; extra.symbol.
- Stubs `x`, `tiktok`, `instagram`: `NEEDS_KEY = "needs key/scraper"`, fetch returns [] and the run report shows them as stubbed. Do not build scraping.

### Layer 2 — state + scoring (zero tokens), SQLite at `state.db`
- Tables (suggested): `items(hash PK, title, url, platform, first_seen, last_seen, published, domains, candidate_at, llm_at)`, `scores(hash, ts, score, rank)`, `sources(name PK, last_fetch, last_status, consecutive_failures, cooldown_until)`, `llm_cache(hash PK, model, result_json, ts)`, `budget(day PK, calls, input_tokens, draft_calls)`, `drafts(hash PK, day, ts)`, `runs(id, ts, summary_json)`.
- Item hash = sha256 of normalized URL (strip scheme, www, query tracking params, trailing slash) — and a second title hash (lowercase, strip punctuation/stopwords) used for dedupe across platforms.
- Velocity = (score_now − score_prev) / hours between observations, normalized per platform (percentile among that platform's items this run). Rank improvement counts as velocity for rank-only sources.
- Keyword match → domains (case-insensitive word-boundary match on title+snippet; `exclude` words veto). `domain_hint` also assigns the domain. Items matching no domain are kept in state but never candidates.
- Cross-platform co-occurrence: build keys from matched domain keywords + salient title tokens (capitalized words / tickers / bigrams minus stopwords). Count distinct platforms sharing a key within the freshness window (default 6h); ≥2 platforms boosts.
- **Freshness/novelty rule (owner requirement):** prefer items < `freshness.max_age_h` (default 6) old with rising velocity — early is the whole point. Score bonus for age < 6h AND velocity > 0; decay penalty beyond; items older than `freshness.hard_max_age_h` (default 48) never become candidates. Age = now − (published or first_seen).
- Composite score in [0,1] = weighted sum of rank_pct, velocity_pct, cooccurrence, freshness (weights in config). Candidate = matched a domain AND never previously a candidate (`candidate_at` null) AND not in llm_cache AND score ≥ the domain's `candidate_score` threshold (default 0.55). Rank candidates by score; keep top `max_candidates` (default 15).

### Layer 3 — classify with Jev (tokens, gated)
**Important: Jev is a System One model. It does NOT generate text; it answers typed questions (Choice / Score / Noul) with probabilities.** So every field is a closed-set judgment; free text is composed by code ("select instead of generate").
- Gate: skip entirely when zero candidates; skip when `--no-llm`/no key; skip when last Jev call was < `llm.min_interval_s` ago (default 3600 — candidates stay pending and are picked up next eligible tick, still "never previously sent"); skip and fall back to no-LLM digest when the daily budget (`budget.max_calls_per_day`, default 30; `budget.max_input_tokens_per_day`, default 400000) is hit.
- ONE request for the whole batch: `state = {"items": [ {"id": "i0", "title", "platforms", "url", "snippet"(≤300 chars), "age_hours", "velocity_note", "domains_guess"}, ... ]}`. Questions keyed `i<n>__<field>` referencing `` `items[n]` `` in the instruction text. Per item:
  - `domain` Choice over enabled domain names + `none`.
  - `lane` Choice: `content` | `trading` | `both` | `skip` (with criteria explaining each).
  - `strength` Score with 5 concrete levels (1 = noise … 5 = major, broadly newsworthy/market-moving now); map score to int 1–5.
  - `reason` Choice over a fixed set (config-editable): launch/release, earnings/financials, regulation/policy, deal/M&A/funding, incident/outage/hack, research breakthrough, price/market move, controversy/drama, viral meme/culture, macro data, other. Code composes `why_trending` (≤20 words) from reason + platforms + velocity + age.
  - `angle` Choice over a config-editable angle set (e.g. explainer, contrarian take, what-it-means-for-X, early heads-up, data breakdown, how-to/opportunity, hot take, none). Code composes `angle` hook (≤15 words) from the angle template + a shortened title.
  - `action` Choice: watch | write-post | research-deeper | trade-review | ignore → `action_hint`.
  - trading fields, asked for every item (speculative fan-out; code consumes only when lane ∈ trading/both): `direction` Choice bullish | bearish | neutral | unclear; `time_sensitivity` Choice minutes | hours | days | none; `asset` Choice over code-extracted candidate tickers/assets (regex `$TICKER`, uppercase 1–5 letter tokens in a known list from domain `tickers` + coingecko symbols seen this run + global `known_assets` in config) plus `none` — only include this question when the item has ≥1 candidate.
- Retry 429/529 with backoff (max 2). Timeout from config (default 30s). Validate every answer is inside its option set; invalid → item marked `malformed`, not cached as ok.
- Record `usage.input_tokens` to the budget table. Cache each item's result by hash in `llm_cache` so nothing is ever re-sent. Store confidence too; in the digest show low-confidence (< `llm.min_confidence`, default 0.5) lane/domain answers with a `?`.
- Model id from config, default pinned `jev-1.13.0`; endpoint default `https://api.typesafe.ai/v1/systemone`.

### Layer 4 — drafts (gated)
- Once per day max `drafts.max_per_day` items (default 3): candidates already classified with strength ≥ 4 and lane ∈ content/both, highest strength×score first, never drafted before (drafts table).
- Because Jev cannot write prose, drafting is a **drafter backend** chosen in config:
  - `template` (default): ONE batched Jev call selecting, per item and per platform, the best hook template and tone from config-editable template sets (Choice questions); code fills templates with title, angle, why_trending, link, voice. Outputs per item: X/Threads post ≤280 chars (truncate safely), short LinkedIn post (≤700 chars), blog outline (title + 5 bullets built from templates: what happened / why now / who it affects / angle / what to watch).
  - `external` (stub): a hook module `hooks/drafter_external.py` with a documented function signature for a future generative LLM; returns None and the digest notes "external drafter not configured".
- Counts against the daily budget (separate `draft_calls` counter plus shared token cap). Skip if budget exhausted.
- Drafts are written to `TW_OUT_DIR/drafts-for-approval/YYYY-MM-DD.md` (append), each block headed `DRAFT — NOT POSTED — needs owner approval`, with item link, lane, strength, and the three drafts. Per-platform templates and voice come from global config, overridable per domain.

### Outputs
- Digest: append a run section to `TW_OUT_DIR/digests/YYYY-MM-DD.md` **only when the run has candidates** (create the file with a small frontmatter `type: deliverable` header on first write of the day). Section: time, per-domain table of candidates (title linked, platforms, age, velocity, score, and when classified: lane, strength, why_trending, angle, action_hint, trading fields). No-LLM runs say "unclassified (no-llm: <reason>)".
- Trading feed: lane ∈ trading/both (or, in no-llm runs, items in domains whose `lanes` include trading) appended as JSONL to `TW_STATE_DIR/feeds/trading/YYYY-MM-DD.jsonl` — one object per item with ts, hash, title, url, platforms, domains, score, velocity, age_hours, assets, direction_hint, time_sensitivity, strength, confidence, classified(bool). Machine-readable, not wired to anything.
- If writing to `TW_OUT_DIR` fails (e.g. the scheduler lacks output-directory permissions), write the same content to `TW_STATE_DIR/outbox/<relative path>` and record a WARN; `trend-watch flush` moves it into place later.
- The run prints its one-line summary to stdout and writes a JSON run summary to `TW_STATE_DIR/logs/last-run.json`.

### Hooks (stubs only)
`hooks/notify.py` (`def notify(summary: dict) -> None: pass`) and `hooks/publish.py` (`def publish(draft: dict) -> None: raise NotImplementedError("publishing is owner-approved only")` — never called by the pipeline; document it). Config keys `hooks.notify_enabled=false`, `hooks.publish_enabled=false`; the pipeline calls notify only if enabled and never calls publish.

### Optional scheduler wrapper
Set `TREND_WATCH_WRAPPER` to an executable script to replace the direct
`bin/trend-watch run` scheduler command. The script can collect stdout/stderr,
append a run report or ledger entry, and propagate the command's exit status.
Resolved `TW_CONFIG_DIR`, `TW_STATE_DIR`, and `TW_OUT_DIR` are provided to either
mode. The Python entry point holds the shared state lock against overlapping runs.

### launchd plist
`setup` generates `~/Library/LaunchAgents/<label>.plist`. The default label is
`io.github.trend-watch`, overridden by `TREND_WATCH_LABEL`. ProgramArguments are
the Python interpreter and checkout CLI with `run`, or the executable custom
wrapper. StartCalendarInterval is `[{Minute:7},{Minute:37}]`, RunAtLoad is false,
and stdout/stderr logs live under `<state>/logs/`. PATH is pinned to
`/usr/bin:/bin:/usr/sbin:/sbin`. `setup --no-enable` installs without enabling;
`enable` and `disable` use launchctl bootstrap and bootout. Linux uses a managed
crontab line with the same schedule and environment.

### Seed domains (in `seed/domains/`, copied by `init`)
`ai-tech`, `markets-trading` (lanes trading+content; tickers SPY QQQ NVDA TSLA AAPL; subreddits wallstreetbets stocks investing; news queries "stock market today", "Fed rate"; polymarket+kalshi; available for downstream research consumers), `crypto` (lanes trading+content; BTC ETH SOL; coingecko, r/CryptoCurrency r/Bitcoin, polymarket), `business-startups` (producthunt, HN, r/startups r/Entrepreneur, news "startup funding"), `culture-viral` (google_trends, r/popular r/OutOfTheLoop r/videos, youtube not-configured). Keep each short and obviously editable.

### README.md
Purpose, layers, token discipline, file locations table, how to add a domain / an adapter (≤15-line example), CLI, enable/disable, budget math, what is stubbed, the TCC/outbox note, "never posts" guarantee.

## Tests (required) — `tests/`, run with `python3 -m unittest discover -s tests`
No network in tests. Cover: URL/title normalization + dedupe; velocity + freshness + composite scoring; candidate selection never re-selects; domain validation skips a bad file without crashing; adapter registry discovers a drop-in adapter from a temp dir; one adapter raising does not fail the run (fake adapters); Jev request builder produces a `questions` map with expected keys and `items[n]` references, ≤ K items, and is skipped on zero candidates; Jev response parser validates option sets and caches by hash (fake HTTP); budget gate falls back to no-LLM; draft templater respects ≤280 chars; outbox fallback when out dir is unwritable; `--dry-run` writes no outputs. Use temp dirs via TW_* env overrides.

Deliver: working code, passing tests, `git status` clean except your new files (do not commit). End with a short summary of files and anything you could not do.
