# Intelligence engine R1: design notes

Design notes for the R1 intelligence engine (SEC EDGAR 8-K intake, staged
engine and the `investment@shadow` consumer). This file records how each
requirement landed in code, where the design was ambiguous or infeasible and
which safe reading was chosen, and the readiness gate for going live.

Nothing in R1 posts, trades, notifies or schedules anything. `hooks/publish.py`
is still unreferenced. Production rendering is disabled: `investment.env` must
be `shadow`, and shadow reports are written under
`<state>/shadow/investment/`, never to a production location.

## 1. Verified SEC facts (fetched 2026-10-04)

| Fact | Source |
|---|---|
| Fair access: at most **10 requests/second** per user, "regardless of the number of machines". Unclassified bots are not allowed. | <https://www.sec.gov/about/developer-resources> (page updated 2025-03-10) |
| "Current max request rate: 10 requests/second." Declared UA format: `User-Agent: Sample Company Name AdminContact@<sample company domain>.com` | <https://www.sec.gov/search-filings/edgar-search-assistance/accessing-edgar-data> (reviewed 2024-06-26) |
| The same UA sample, plus `Accept-Encoding: gzip, deflate`. "Latest Filings search and associated RSS" are the closest to real time. | <https://www.sec.gov/about/webmaster-frequently-asked-questions> (updated 2024-08-23) |
| **Live check:** a request with a declarative UA that had **no contact email** got **HTTP 403 "Your Request Originates from an Undeclared Automated Tool"** (Akamai, 2026-10-04 23:46 UTC). The placeholder refusal is therefore a functional requirement, not just policy. | one direct `curl` of `https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&…&output=atom` |
| **Feed shape** (read once with a generic fetch; no contact email was sent): Atom, `encoding="ISO-8859-1"`. Each `<entry>` has `<title>8-K - <Company> (<10-digit zero-padded CIK>) (Filer)</title>`, `<link rel="alternate" href=".../Archives/edgar/data/<CIK>/<18-digit accession>/<dashed accession>-index.htm"/>`, `<summary type="html">` with `Filed: YYYY-MM-DD AccNo: NNNNNNNNNN-NN-NNNNNN Size: …` and one `Item N.NN: <heading>` line per reported item, `<updated>`, `<category label="form type" term="8-K"/>`, and `<id>urn:tag:sec.gov,2008:accession-number=NNNNNNNNNN-NN-NNNNNN</id>`. | `getcurrent` Atom feed |
| **Item numbers are in the summary.** The feed has **no field naming the filing an 8-K/A amends.** | same |
| **Conditional GET (ETag / Last-Modified) is not documented** on any of the three pages, and it could not be verified live because the direct fetch was refused. | — |

How the code uses these facts (`trend_watch/intake/sec_edgar.py`):

- `request_interval_s` must be ≥ 0.1 (validated). The `RateLimiter` spaces requests by at least that much.
- The UA is configurable (`intake.sec_edgar.user_agent`). The seed ships `<CONTACT_EMAIL_REQUIRED>`. A live fetch (no injected opener) raises `ConfigError` unless the UA contains a real-looking email that is not `example.invalid`.
- Conditional GET is opportunistic. Stored validators are sent as `If-None-Match`/`If-Modified-Since`; a 304 means "not modified". If SEC sends no validators, nothing is stored and nothing breaks.

## 2. Architecture as built

The stages are `ingest → resolve → annotate → consume <name> → render`. `tick` runs them in that order. They live in `trend_watch/engine.py`; each runs in its own failure domain via `run_stage` and is recorded in the `stage_runs` ledger: uid, tick_uid, stage, scope, stage_version, start/finish, status (`success|partial|skipped|failed`), counts_json, error and prerequisite_run_id.

Every mutating CLI entry point takes one global lock: `run`, `tick`, every stage, `ack`, `migrate`, `backup`, `restore`, `jev on/off`, `domains add/archive`, `flush`, `init`, `setup`, `enable`/`disable`, `budget` and `sources`. It is an `flock` on `<state>/trend-watch.lock` (`trend_watch/lock.py`, re-entrant in-process). Scheduler-facing commands fail fast with exit 75; interactive ones wait up to 30 s. **All mutating stages are globally serialized**; WAL only helps readers.

Data model (migration v3; all tables use integer surrogate PKs):

| Layer | Tables | Immutability |
|---|---|---|
| Observations | `source_items(source, external_id)`, `observations(source_item_id, payload_hash)` | Triggers reject UPDATE/DELETE on `observations`. |
| Events | `events(lane, cik, accession)`, `event_revisions(event_id, revision_no / content_hash)`, `event_entities` | Event identity columns are trigger-guarded; revisions and entities are immutable. |
| Annotations | `annotations(producer, producer_version, input_hash)`, `annotation_attempts`, `annotation_queue`, `llm_reservations`, `dependency_episodes` | Annotations are immutable. Attempts move only forward through the state machine. |
| Evaluations | `evaluation_requests`, `evaluation_attempts` | Both immutable. |
| Records | `records`, `record_supersessions`, `record_heads`, `acks`, `render_log` | Records, supersessions and acks are immutable; `record_heads` is the only active-pointer projection. |
| Intake | `intake_admissions` (projection), `intake_log` (append-only), `breaker_trips` (append-only) | — |

External UIDs follow `tw:<namespace>:<first 32 hex of sha256>` over a length-prefixed canonical encoding (`ids.encode`: `s<len>:<utf8>`, `i<int>;`, `n;`, `l<n>:…`). Environment is never an input to an event or observation UID; it appears only in consumer records and evaluations. Investment event identity is `uid("event", lane, int CIK, dashed accession)`.

## 3. The 12 spec fixes as implemented

### Fix 1: Observations

- `observations.record()` upserts `source_items` (`sightings+1`, `last_seen_at`). It inserts an observation only when `payload_hash` (sha256 of canonical JSON) is new for that source item.
- An identical refetch updates only `last_seen_at`/`sightings`. A changed payload creates a new observation, which can create a revision.
- The caller owns the transaction (see fix 10 for the ETag coupling).
- **Every legacy adapter fetch** is observed in `pipeline.run` right after `fetch_all`, before scoring or candidate selection, in one transaction.
  - Legacy source = adapter name; `external_id` = normalized URL.
  - Payload = content fields only (title, url, snippet, published, domain_hint). Volatile metrics (score/rank/prices) stay in the existing `scores` time series, so a vote-count change is not a new observation.
  - A failure is rolled back and reported as `observations: <Error>`. It never stops the trend path.
- EDGAR: `external_id = "<dashed accession>|<CIK as published>"`, because co-registrants list one accession under several CIKs.

### Fix 2: Amendments

- Every accession is its own observation and event.
- `resolve` links an 8-K/A **only** through the deterministic source field `amends_accession`. If that field is present and the target `(CIK, accession)` event exists, the event is created with `amends_event_id` set and `amendment_status='linked'`.
- If the target is missing, or the field is absent, the event is created with `amends_event_id = NULL` and `amendment_status='unresolved'`. The `amendment_basis` records why. Title similarity is never used.
- Late link (the original arrives after the amendment):
  - `events.amends_event_id` may move only NULL→value (trigger-enforced).
  - A new **revision** (`reason='amendment_linked'`) carries `amends_event_uid`.
  - The consumer then evaluates the new input as a new request, and the new record supersedes the old one.
  - Prior evaluations and records are never rewritten.
- Semantics are link + supersede, never merge. Both events remain; non-amendments get `not_amendment`.
- **Spec deviation (safe interpretation):** the getcurrent Atom feed has no amends field, so every live 8-K/A resolves as `unresolved` in R1. The link path is implemented and tested with synthetic observations carrying `amends_accession`, ready for a future structured source.

### Fix 3: Evaluation identity and replay

- `evaluation_requests` is UNIQUE on (consumer, env, revision_id, consumer_version, ruleset_version, input_hash, replay_batch_id). `input_hash` is the sha256 of the persisted input snapshot.
- An identical input whose request already reached `done` is an **idempotent no-op**.
- An explicit replay (`consume <name> --replay-batch ID`) creates a new tagged request (`trigger='replay'`). It creates a new record only if the body differs.
- A changed input creates a new request (`trigger='input_changed'`) and marks older open requests `superseded`. Causes include a new revision, a recovered annotation, or a watchlist edit (`watchlist_version` is part of the snapshot).

### Fix 4: Processing status is separate from the decision

- `evaluation_attempts` stores `processing_status` (`done | awaiting_annotation | blocked_dependency | deferred_budget | superseded`) separately from `decision` (REVIEW/IGNORE/INCOMPLETE, set only when `done`) and `reason_code`.
- Lane admission statuses:
  - `admitted`
  - `deferred_cap`: `eligible_at` = next UTC midnight
  - `deferred_anomaly`: `eligible_at` = breaker pause expiry
- Deferred items are re-admitted once eligible. Nothing is dropped.
- The only terminal outcome is malformed input. `resolve` writes `observation_resolutions.status='malformed'` plus a `dead_letters` row.
- History stays: every status change appends an attempt row.

### Fix 5: Stage prerequisites

- `annotate` and `consume` check the most recent `resolve` run. If it failed, they are recorded as `skipped`, so unresolved observations are never treated as processed. Example: an invalid watchlist fails `resolve`.
- A failed `ingest` still lets the later stages process committed work.
- A failed `annotate` does not block `consume`; dependent evaluations stay `awaiting_annotation`/`blocked_dependency`.
- Each consumer is its own `run_stage` and its own failure domain.
- A failed `render` leaves records in place for the next render.

### Fix 6: Records and acks

- Records are immutable. `record_heads` (PK consumer, env, event_id, kind) guarantees exactly one active record.
- A new record is created only when the body hash differs from the head. It is linked by `record_supersessions(old UNIQUE, new UNIQUE, CHECK new>old)`, which makes the chain one-to-one and acyclic.
- Record UID = `uid("record", consumer, env, event_uid, kind, request_uid)`.
- `ack <record_uid> [--actor] [--note] [--verdict correct|incorrect] [--correct DECISION] [--env]` appends to the immutable `acks` table.
- Re-acking without a note or verdict is an idempotent "already acknowledged".
- Errors: unknown record, wrong env, invalid verdict/correction, or empty actor.
- Acking a superseded record works and names the successor. Acks never propagate.
- Superseded records stay visible in the report.

### Fix 7: Paid-attempt state machine

Lives in `trend_watch/annotator.py`. States: `reserved → sending → response_received → validated | ambiguous`, plus `reserved/sending → not_sent | ambiguous`.

- The budget reservation and the `reserved` attempt row are written in **one** `BEGIN IMMEDIATE` transaction.
- `sending` is committed before transport.
- Only plausibly-sent attempts (`ambiguous`) count toward `max_ambiguous_attempts` (2):
  - `ambiguous`: timeout, reset, 500/504/other 4xx, malformed or invalid answers.
  - `not_sent`: connection refused/DNS, 429/502/503/529, 401/403, crash before send. These release their reservation.
- After 2 ambiguous attempts an `unavailable` annotation is written, and the rules decide `REVIEW` with `reason_code='annotation_unavailable'`. The report says "materiality annotation unavailable"; it never claims positive materiality.
- Unit of billing: one request per filing; each request is one bounded question set of two Choice questions.

### Fix 8: No fallback (permissive by design)

- If Jev is disabled, the key is missing, or Jev is down or rate-limited:
  - Deterministic stages and decisions continue.
  - LLM-dependent evaluations become `blocked_dependency`.
- `dependency_episodes` opens one episode, reported once (stderr line `BLOCKED: …`, plus a line in the report).
- Each annotate run sends at most one **probe** to the same backend. Success closes the episode and resumes.
- No other model and no hand-written substitute exists in the code path. `ScriptedJev` is a test/replay *transport* only and requires an injected client factory.

### Fix 9: Retention vs audit

The exact normalized decision input is stored with each request (`input_snapshot_json`): revision content, decisive CIK, lexical evidence, `watchlist_version`, amendment state and the terminal annotation. Replay and audit therefore do not depend on any future retention of observations. No retention or VACUUM job was added in R1.

Post-R1 (merged onto main with R1): `State.prune` plus the seed `retention` block delete only the legacy time-series tables `signals` (2 days), `scores`, `runs` and `llm_calls` (30 days), then VACUUM when due. Nothing it reads is older than one UTC day (`signals` within `cooccurrence_h`, `scores` latest row, `llm_calls` per-day caps and min-interval). Observations, R1 tables and every immutable table are never pruned, so this fix still holds.

### Fix 10: Migrations, backup and restore

- `trend_watch/migrations.py`: v1 = baseline schema, v2 = P0 + observations + ledger, v3 = engine.
- Order of operations:
  1. Opening an existing DB with pending migrations first takes an online-API backup (`backups/pre-migrate-vX-to-vY-<UTC>.db`, integrity-checked).
  2. Each migration then commits in `BEGIN IMMEDIATE` with its `schema_version` row.
  3. **Then** `journal_mode=WAL`.
- A newer schema is refused (`SchemaTooNew`). Read-only probes (`status`) open with `migrate=False`.
- Tests:
  - A DB built with the exact baseline SQL upgrades to v3, keeps its legacy rows uninterpreted, and gets the pre-migration backup.
  - A failing migration rolls back to the previous version.
- `backup`: online backup to `<state>/backups/state-<UTC>.db`.
- `restore <file>`: verifies integrity and schema, archives the current DB to `backups/pre-restore-…` (never deletes), restores, then forward-migrates.
- **Conditional fetch** (ETag/Last-Modified) is written in the same transaction as the observations. A crash cannot advance the checkpoint past uncommitted observations (tested).
- **Not done:** the backup *schedule* (trend-watch never installs schedules itself). The operator must schedule `trend-watch backup`.

### Fix 11: Budget semantics

- **UTC budget day.** One global budget: legacy `budget` row + `llm_reservations` in state `held|consumed`. Legacy `budget_reason` now includes shadow usage.
- **Priority:** legacy trend workload > shadow workload. A shadow reservation fails if it would leave fewer than `budget.legacy_reserve_calls` of `max_calls_per_day`, or exceed the global token budget, or exceed `budget.kinds.materiality.{max_calls_per_day,max_input_tokens_per_day}`.
- **Fairness within shadow:** FIFO by (filed_date, accession, event id).
- **Token reconciliation:** reserve `estimated_input_tokens_per_call`, then reconcile to `usage.input_tokens` when reported. If not reported, the estimate stands.
- Budget denial → `deferred_budget` until next UTC midnight; no attempt row is created.
- **Crash between reservation and response:** on the next annotate run, `reserved` → `not_sent` with the reservation released; `sending`/`response_received` → `ambiguous` with the reservation consumed.
- There is no local-time 18:00 release; everything is UTC.

### Fix 12: Tripwire

- `trend_watch/tripwire.py` and `trend-watch scan [paths] [--legacy]`. A hit is any run of 12 or more digits; runs of 12–19 always fall inside that.
- A value is exempt only if its **field name** is in `tripwire.exempt_fields` **and** the value matches the type. Raw external fields in `tripwire.never_exempt` are always scanned, even when they contain a URL.
- In rendered Markdown, only renderer-produced link targets `](https://…)` and back-quoted digest UIDs count as typed fields. The renderer escapes `[ ] ( ) \` ` | < >` in external text, so neither form can be forged (tested with a hostile company name).
- Default scan: shadow reports, `feeds/`, and the R1 tables of `state.db`. The fixture replay scans each case's outputs.

Exempt typed fields (also listed in `seed/config.json` → `tripwire`):

| Type | Value must match | Fields |
|---|---|---|
| `edgar-accession-dashed` | `^\d{10}-\d{2}-\d{6}$` | `accession`, `amends_accession` |
| `digest-uid` | `^tw:[a-z_]+:[0-9a-f]{32}$` | `uid`, `event_uid`, `record_uid`, `revision_uid`, `request_uid`, `supersedes_uid`, `amends_event_uid`, `observation_uid`, `watchlist_version`, `tick_uid`, `fetch_uid` |
| `hex-digest` | `^[0-9a-f]{64}$` | `hash`, `title_hash`, `item_hash`, `payload_hash`, `input_hash`, `content_hash`, `body_hash` |
| `url` | http(s), has a host, no whitespace | `url`, `link`, `feed_url` |
| never exempt | — | `snippet`, `summary`, `title`, `company`, `label`, `note`, `raw`, `error` |

An undashed 18-digit accession in an `accession` field is **not** exempt; the type check fails.

## 4. P0 hardening (legacy path otherwise frozen)

- **Float rounding (6 dp), serialization only.** Applies to `items.data_json`, `pending`, `llm_cache`, the trading-feed JSONL, `runs`, `last-run.json`, and all R1 JSON. Scoring math is unchanged.
- **Per-kind Jev gates.**
  - `last_call(kind)`: draft calls no longer delay classification.
  - `llm.kind_min_interval_s.{draft,materiality}`.
  - `budget.kinds.<kind>.max_calls_per_day` is checked on top of the global budget.
- **Versioned Jev cache key.** `llm_cache.cache_version='classify/1'` on new rows. Pre-R1 rows keep NULL and are honoured exactly as before.
- **New config sections in `seed/config.json`.** `intake`, `investment`, `tripwire`, `budget.kinds`, `budget.legacy_reserve_calls` and `llm.kind_min_interval_s` are validated by `validate_shape` (unknown keys are still rejected) plus `validate_r1`. No domain-level keys were added, so `DOMAIN_KEYS` is unchanged.
- **Baseline test change.** `test_dry_run_has_only_db_history_and_no_outputs` now accepts `state.db-wal`/`state.db-shm`. Apple's system SQLite (3.51) persists WAL sidecars after close.

## 5. Consumer rules (`trend_watch/consumers/investment.py`, ruleset `8k-items/1`)

The first matching rule wins:

1. If the form is not 8-K or 8-K/A → IGNORE `form_out_of_scope`.
2. If there are no items → INCOMPLETE `items_missing`.
3. If any item is a review item → REVIEW `review_item`. Review items are 1.01–1.05, 2.01–2.06, 3.01–3.03, 4.01, 4.02, 5.01, 5.02.
4. If any item is unknown → REVIEW `unrecognized_item`.
5. If any item is 7.01 or 8.01 → ask the Jev question set:
   - `material` or `unclear` → REVIEW.
   - `not-material` → IGNORE.
   - Unavailable → REVIEW `annotation_unavailable`.
6. Otherwise (only 5.03–5.08, 9.01, 6.0x) → IGNORE `routine_items_only`.

Confidence is stored as `confidence_reported` and never read.

Watchlist (`<config>/<investment.watchlist_path>`, example `fixtures/edgar-8k/watchlist.json`, CIK 320193):

- The CIK is decisive.
- Aliases match case-insensitively on word boundaries (NFKC-normalized). Tickers match as `$TICK`, `EXCHANGE:TICK`, `(TICK)` or a bare uppercase token.
- `ambiguous_ticker` entries (ON, IT, ALL…) match only as `$TICK` or `EXCHANGE:TICK`.
- Lexical matches are stored as `event_entities.decisive=0` evidence and **never admit**.
- A missing watchlist means an empty watchlist (no intake). An invalid watchlist fails `resolve`.

First sighting: a watchlisted primary-source filing is admitted and evaluated on the tick it is first observed. No trend score is involved; tested with `candidate_score=0.99`.

Circuit breaker (per consumer@env):

- **Observed:** lane events created today (UTC), counted since the later of midnight and the last pause expiry.
- **Baseline:** median of the previous `baseline_days` complete UTC days. Zero-count days are included; days before the first event are not.
- **Threshold:**
  - With at least `min_history_days` of history: `min(absolute_ceiling, max(min_trip_count, multiplier × median))`.
  - Otherwise: `absolute_ceiling`.
- **On trip:** pause for `pause_s`, with an append-only `breaker_trips` row and a stderr line. Filings stay ingested and resolved; admission is deferred.

## 6. Spec cross-check: issues and the safe interpretation taken

1. **No deterministic amends field in the feed.** All live 8-K/A are `unresolved` (see fix 2). Linking is ready for a structured source.
2. **The feed carries only item headings, so the LLM evidence is thin.** The bounded question judges headings only; most 7.01/8.01 filings will come back `unclear` → REVIEW. Fetching the primary document or exhibit 99 is deferred (it would be a second SEC request type and needs its own budget).
3. **Legacy hex hashes and floats in state.db.** 64-hex digests (`items.hash` etc.) can contain 12+ digit runs; they are covered by the typed `hex-digest` exemption. Pre-R1 `data_json` rows keep their unrounded floats, because rows are frozen and never rewritten. `scan --legacy` will report them; the default scan covers R1 outputs.
4. **The 14-day shadow and the backup "schedule" both need a schedule, which trend-watch never installs itself.** None is added, and `ingest` stays disabled by default. Readiness cannot pass until the operator:
   - sets the UA email;
   - enables `intake.sec_edgar.enabled`;
   - creates a watchlist;
   - schedules `trend-watch tick` (and `backup`).
5. **Co-registrants.** "Investment event identity = (CIK, accession)" means one accession filed by several CIKs becomes several events. Only watchlisted CIKs are admitted (fixture `co_registrant_same_accession`).
6. **Annotation identity.** An early draft keyed annotations on headings only, which let two filings with identical headings share one LLM answer. Fixed: the annotation `input_hash` includes CIK and accession.
7. **Conditional GET is unverified** (§1). It is implemented as best-effort.
8. **Optional notification transport outage** (fix 8 mentions it): trend-watch has no notification transport dependency in R1, so only the Jev backend is covered.
9. **Read-only commands on a fresh machine** (`status`, `domains validate`) may still create the initial config non-destructively, outside the lock. This is baseline behaviour and was kept.
10. **WAL sidecars persist on macOS** (§4). One baseline assertion was adjusted.

## 7. Readiness gate (`trend-watch readiness`, read-only)

All four checks must pass before production rendering is even considered. Passing changes nothing by itself.

1. `replay` passes: every fixture label matches, no exceptions, no tripwire hits.
2. At least `investment.readiness.min_shadow_days` (14) distinct UTC days with a successful `consume:investment@shadow` run.
3. At least `min_spot_checks` (20) operator verdicts on REVIEW records (`ack --verdict correct|incorrect [--correct DECISION] --note …`).
4. Operator REVIEW precision (correct / verdicts, latest verdict per record) is at least `min_review_precision` (0.8).

Then, separately, the operator chooses a production output folder and approves go-live. That is a code and config change in a later release; R1 rejects `investment.env != "shadow"`.

## 8. Commands

```sh
trend-watch tick                  # ingest -> resolve -> annotate -> consume -> render (locked)
trend-watch ingest|resolve|annotate|render
trend-watch consume investment [--replay-batch ID]
trend-watch records [--all]       # read-only list with record UIDs
trend-watch ack <record_uid> --verdict correct --note "checked"
trend-watch replay [--json]       # offline fixtures, no network, no live Jev
trend-watch scan [paths] [--legacy]
trend-watch readiness
trend-watch migrate | backup [--dest DIR] | restore <file>
```
