# Example pack schema

`example.json` describes the fictional Orbital Dispatch. Copy it together with
`domains/` into a configured pack directory, or use `--packs-dir examples/packs`.
Names are lowercase slugs (letters, digits, `_`, `-`), starting with a letter;
`list` is reserved by the CLI. Each file is `<name>.json`. JSON has no comments;
`_help` fields are documentation and are ignored by the engine.

Every engine setting is listed below. Unless marked optional, provide it as in
`example.json`. Live `<config>/<name>.json` overrides merge nested maps and
replace lists. Unknown extra metadata is ignored by the pack engine.

| Key | Meaning |
|---|---|
| `enabled` | Evaluate and collect this pack; `--test-nudge` can evaluate a disabled pack. |
| `title` | Optional alert/report title; defaults to the pack name. |
| `domains` | Domain filenames without `.json`; resolve beside the pack, then in repository `seed/domains/`. |
| `labels` | Optional map from domain name to display label; falls back to the name. |
| `defer_to` | Optional other pack names; matching recent successful alerts hold this pack's story. Default `[]`. |
| `finance_first_terms` | Optional case-insensitive whole terms that hold a story if present in the lead headline. Default `[]`. |
| `finance_domain_terms` | Optional whole terms that hold a story if present in any clustered headline. Default `[]`. |
| `weights` | Numeric weights summing to 1 for `velocity`, `relevance`, `source_quality`, `novelty`, `persona_fit`. The last key is the compatible spelling for audience fit. |
| `alert.threshold` | Base score bar in `(0, 1]`. |
| `alert.max_story_age_h` | Maximum age in hours, measured from the oldest member. |
| `alert.min_outlets`, `alert.min_tier1_outlets` | Required fresh outlet count and tier-1 outlet count. |
| `alert.max_nudges_per_run`, `alert.max_nudges_per_24h` | Caps on ordinary alerts; test nudges bypass these caps. |
| `alert.dedup_h` | Lookback for hash/signature deduplication, including deferred packs. |
| `alert.market_move.pct_1h`, `alert.market_move.pct_24h` | Absolute CoinGecko percentage moves that corroborate a story; either suffices. |
| `alert.market_move.min_outlets` | Fresh outlet requirement when a matching market move satisfies coverage. |
| `window.lookback_h` | Shared-state item lookback. |
| `window.fresh_h`, `window.burst_h` | Fresh and burst windows used to measure outlet density. |
| `cluster.min_shared`, `cluster.min_overlap` | Minimum shared headline tokens and overlap fraction to join a cluster. |
| `news_platforms` | Platforms whose items form news clusters. |
| `relevance` | Map of domain to lists of term groups; every group needs at least one whole-term headline hit. |
| `strong_terms` | Whole terms that increase relevance. |
| `persona_markers` | Compatible key for audience-fit substrings, matched in lowercase headlines; money figures also count. |
| `stale_patterns` | Lowercase substrings that penalize novelty in the first three cluster headlines. |
| `outlets.tier1`, `outlets.tier2` | Lowercase outlet names; unmatched outlets use tier 3, social sources tier 4. |
| `nudge.command` | List of argv strings. `[]` means local outbox only. `{marker}`, `{action}`, `{file}` are substituted; the file path is appended if it is not already a standalone argument. No shell evaluation. |
| `nudge.marker`, `nudge.action` | Command values for an alert; also identify outgoing envelopes during harvest. |
| `nudge.report_marker`, `nudge.report_action` | Command values for the automatic first report. |
| `nudge.first_report_after_h` | Hours from first live evaluation to the automatic report; zero disables it. Successful delivery or outbox write marks it complete. |
| `nudge.timeout_s` | Command timeout in seconds; also bounds outgoing-envelope time matching. |
| `nudge.max_sources` | Maximum distinct-outlet source links in an alert. |
| `tuning.enabled` | Optional, default false; enables reply harvesting and the tuned bar. |
| `tuning.link_state` | Optional reply-state directory, default empty; `~` is expanded. The key remains compatible with existing packs. |
| `tuning.peer` | Optional peer name, default empty. Both source and peer must be set to harvest. |
| `tuning.window` | Recent verdict count, default 10. |
| `tuning.step_up`, `tuning.step_down` | Per-miss increase and per-draft decrease; defaults 0.02 and 0.01. |
| `tuning.max_up`, `tuning.max_down` | Maximum positive/negative bias; defaults 0.10 and 0.05. Final bar is bounded to `[0.05, 1]`. |

The engine derives `pack`, `pack_file`, `domain_files`, `finance_first`,
`finance_domain`, and `_bar` at runtime; do not set them in JSON.

Reply sources are optional and read-only. Outgoing envelopes live at
`<link_state>/lanes/<peer>/outbox/*.json` or its `sent/` child, with `action`,
`marker`, `seq`, `event_id`, and UTC `emitted_at` (`YYYY-MM-DDTHH:MM:SSZ`). Replies
live at `<link_state>/threads/<event_id>/transcript.jsonl`. An entry with
`dir: "peer"`, `marker: "DONE"`, `text` containing `drafted`, `already-covered`
(or `already covered`), or `stale`, and optional UTC `at` records a verdict.
Only sent ordinary alerts from the last 14 days without a verdict are harvested.
Repeated harvest is idempotent; absent/disabled sources do nothing.

Domain JSON uses the normal collector schema: optional `name` (filename stem),
`enabled`, `keywords`, `exclude`, `sources` (adapter parameter maps), `lanes`
(`content`/`trading`), `tickers`, `thresholds`, `drafts`, `freshness`, `weights`,
and `max_candidates`. All except `name` inherit `config.json` defaults; see
[the main README](../../README.md#add-or-retire-a-domain). `_help` is ignored.
Local domain edits in `<config>/domains/` win; archived domains stay retired.
