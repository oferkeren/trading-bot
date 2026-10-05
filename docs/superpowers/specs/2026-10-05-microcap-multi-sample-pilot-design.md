# Micro-cap Multi-sample Coverage Pilot and Focused Dashboard — Design

Date: 2026-10-05
Status: approved

## Goal

Move Phase 1 research from one hand-picked sample (SORA, 2026-10-01) to a mechanically
selected batch of up to 20 recent micro-cap runner days. Measure how often each data
source (IBKR minute bars and quotes, Massive dated roster, Alpaca news, SEC filed shares)
actually covers those samples. Show the result on a redesigned dashboard that contains
only the micro-cap strategy and the safety controls.

Everything stays research-only: decision is always `NO_TRADE`, `order_approval` and
`model_calibrated` are false, `target_probabilities` is `"unavailable"`. No order,
account, or scanner code paths are touched.

## Non-goals

- No probability calibration, entry rules, or Phase 2/3 work.
- No verification of roster completeness or decision-time market cap; those blockers stay.
- No scheduled/automatic runs; batches are started manually.
- No change to bot trading behavior. Removed dashboard sections keep their backend routes.

## 1. Selection — `microcap_batch_select.py`

CLI: `--as-of YYYY-MM-DD` (default: latest completed US trading day),
`--days 10`, `--per-day 2`, `--output /absolute/external/batch-manifest.json`.
Requires `MASSIVE_API_KEY` and `SEC_USER_AGENT` in the environment.

1. Walk back calendar days from `--as-of`. For each day, call Massive
   `GET /v2/aggs/grouped/locale/us/market/stocks/{date}?adjusted=false`. A day is a
   trading day only if `resultsCount > 0`. Stop after `--days` trading days or 21
   calendar days, whichever comes first (fewer days is reported, not an error).
2. Screen each day: open `o` in [$1, $20], volume `v` >= 1,000,000,
   move = `h / o - 1` >= 0.30. Rank by move descending, ties by ticker ascending.
3. Walk the ranked list until `--per-day` samples are accepted for that day. A candidate
   is accepted only if:
   - Massive `GET /v3/reference/tickers/{ticker}?date={date}` returns `type == "CS"`;
   - the ticker maps to a CIK in SEC `https://www.sec.gov/files/company_tickers.json`
     (downloaded once per run with `SEC_USER_AGENT`);
   - that CIK is not already selected in this batch.
   Every examined candidate is recorded with an outcome: `SELECTED`,
   `NOT_COMMON_STOCK`, `NO_SEC_CIK`, `DUPLICATE_ISSUER`, or `PROVIDER_ERROR`.
   At most 10 candidates are examined per day.
4. Massive calls are paced to at most 5 per rolling minute.
5. Each selected sample: `issuer_id` = 10-digit CIK, `symbol`, `company` (SEC title),
   `date`, `move_pct` (rounded to 0.1), and the regular session window
   09:30–16:00 America/New_York converted to UTC (`start_utc`, `end_utc`).
   The SEC decision time is `start_utc`.
6. Manifest fields: `schema_version: 1`, `kind: "microcap_batch_manifest"`,
   `created_at`, `as_of`, `rule` (days, per_day, min_open, max_open, min_volume,
   min_move, max_candidates_per_day, symbol_pattern `[A-Z]{1,6}`), `trading_days` (date + Massive `request_id`),
   `candidates` (audit rows above), `samples`, `bias`
   (`["RUNNER_SCREEN_SELECTED", "SELECTION_USES_SAME_DAY_OUTCOME", "UNVERIFIED_PILOT"]`),
   and `sha256` over the canonical JSON (sorted keys, no whitespace) of every other field.
7. `SELECTION_USES_SAME_DAY_OUTCOME`: the screen uses the day's high, which is not known
   at the open. These samples are valid for data-coverage measurement only and must
   never be used to calibrate or evaluate entry rules.
8. No market-cap filter (unverifiable); shares stay `MARKET_CAP_UNVERIFIED`.
9. Output path must be absolute and outside the repository (same `_external` rule as
   other research CLIs); written atomically. Exit 0 ok, 2 invalid input, 3 provider error.

## 2. Runner — `microcap_batch_run.py`

CLI: `--manifest /abs/batch-manifest.json --out-dir /abs/external/dir [--resume]`.
Requires the same private env as the underlying CLIs (`IB_PROBE_CLIENT_ID`,
`MASSIVE_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`, `SEC_USER_AGENT`).
Credentials are never passed as arguments or written to output.

1. Verify manifest `kind`, `schema_version`, structure, and `sha256`; refuse on mismatch.
2. For each sample in manifest order, in `<out-dir>/<date>-<cik>/`:
   1. Write `roster.json` (one member: CIK issuer, symbol, company, `valid_from` =
      date 00:00Z, `coverage_claim: "pilot-unverified"`, `source_url` = SEC tickers
      URL) and `manifest.json` (`predeclared`, one issuer, one date).
   2. IBKR: `microcap_coverage_probe.py --roster … --sample-manifest … --start
      --end --pilot --fetch --max-symbols 1 --max-days 1`, stdout to
      `ibkr-report.json`. Exit 0 → `OK`; exit 3 with a valid JSON report on stdout →
      `PROVIDER_ERROR` but the report is kept (known IBKR news-window case).
   3. If an IBKR report exists: add `sample_manifest.symbol`, write `base-report.json`,
      then `microcap_source_probe.py --fetch --max-pages 2` → `source-report.json`.
      Otherwise this stage is `SKIPPED`.
   4. SEC (independent of IBKR): `microcap_sec_probe.py --cik --decision-at start_utc
      --fetch --output sec-report.json`.
   5. Write `sample-result.json` atomically: per-stage status (`OK`,
      `PROVIDER_ERROR`, `INVALID`, `TIMEOUT`, `SKIPPED`) and a summary row (below).
      Raw stderr is never stored; only fixed codes.
3. Each subprocess has a 300 s timeout. Subprocesses inherit the environment; the
   runner itself never reads credential files.
4. At least 25 s between the start of consecutive samples (Massive 5/min).
5. `--resume` skips samples whose `sample-result.json` exists and is valid.
6. After the last sample, write `batch-report.json` atomically: manifest `sha256`,
   `as_of`, `rule`, `bias`, `coverage` counts, `samples` summary rows, `blockers`,
   and the fixed `NO_TRADE` fields.

Summary row: `date`, `symbol`, `issuer_id`, `move_pct`, `ibkr_minute` (bool),
`ibkr_quotes` (bool), `roster` (`DATED_ROSTER_OBSERVED` / `MISSING` / provider code),
`news_count` (int or null when not observed), `sec_observations` (int),
`stage_errors` (sorted list of `<stage>:<status>` for non-OK stages).

Coverage counts (each `{observed, total}`): `ibkr_minute`, `ibkr_quotes`,
`roster_dated`, `news_found` (news_count >= 1), `sec_shares` (sec_observations >= 1).

## 3. Snapshot schema 2

`microcap_readiness.py` gains `--batch-report /abs/batch-report.json` (mutually
exclusive with `--source-report`). It publishes a schema 2 snapshot:

```
schema_version: 2, generated_at, decision: "NO_TRADE", order_approval: false,
model_calibrated: false, target_probabilities: "unavailable",
batch: {as_of, manifest_sha256, sample_count, rule, bias},
coverage: {ibkr_minute, ibkr_quotes, roster_dated, news_found, sec_shares},
samples: [ ≤20 summary rows ],
blockers: [ sorted allowlisted codes ]
```

Validation: allowlisted keys only; counts are non-negative ints with
`observed <= total == sample_count == len(samples)`; coverage recomputed from rows must
match; no duplicate (`date`, `issuer_id`); `issuer_id` is 10 digits; `symbol` matches
`[A-Z]{1,6}` (class/unit symbols such as `BRK.B` are excluded at selection); stage error codes and blockers from fixed allowlists; bias exactly
the three labels. No prices beyond `move_pct`, no article text, URLs, share counts, or
credentials. Paths external, atomic write. Schema 1 publishing is unchanged.

`microcap_readiness_store.read_readiness` validates schema 1 and schema 2 strictly;
anything else returns the sanitized `UNAVAILABLE` fallback. `CURRENT`/`STALE` (7 days)
rules unchanged. `/microcap-research-status` route unchanged.

## 4. Focused dashboard (layout B)

- Current live `dashboard.html` is preserved as `dashboard_legacy.html`, served by a new
  authenticated `GET /dashboard-legacy`. Kept until the user asks to delete it.
- New `dashboard.html`, served by the existing authenticated `GET /dashboard`.
- Top bar (always visible): brand, PAPER/LIVE badge (`GET /trading-mode`), NO TRADE
  research badge, IBKR connection dot (`broker.tws_connected` from `/status`; grey
  when missing or older than 60 s per `broker.snapshot_age_seconds`), kill switch (same `POST /control/kill-switch`
  with confirmation modal).
- Sidebar tabs, selected tab kept in the URL hash:
  1. Research (default): schema 2 coverage KPIs + sample table; schema 1 single-sample
     view as fallback; `UNAVAILABLE` state otherwise.
  2. Positions: managed and broker positions (`/dashboard-summary`,
     `/dashboard-portfolio`).
  3. Orders: open orders (`/dashboard-portfolio`).
  4. Health: runtime components (`/dashboard-components`), safety panel, and the
     existing PAPER/LIVE switch with its current confirmation flow.
- Removed from the page only: Strategy Pipeline, IBKR Top Gainers, Current Scanner
  Universe, Hot Pool, Trading Day & Risk, Systemd Services, Recent Signals, Event Stream,
  strategy-mode selector. Backend routes stay.
- Vanilla HTML/CSS/JS, no external CDNs. Data rendered with `textContent` only.
  Per-card `UNAVAILABLE` on fetch failure. Polling: top bar and active tab every 5 s,
  Research every 60 s. Renderers live in `microcap_research_panel.js` (extended for
  schema 2) and a new `dashboard_app.js` served by a fixed authenticated route
  `GET /dashboard-app.js`.

## Error handling

- Provider failures isolate to one sample stage and are visible as stage codes.
- Invalid manifests, edited manifests, or inconsistent batch reports fail closed (exit 2,
  no snapshot written).
- Dashboard shows `UNAVAILABLE` per card; the kill switch does not depend on research
  or portfolio fetches succeeding.

## Testing

- Unit tests (unittest, no network): screen and ranking, candidate outcomes, pacing
  (injected clock), session-window DST conversion, manifest hashing and tamper
  rejection, runner stage mapping with fake subprocess runner, resume, batch-report
  aggregation, schema 2 projection/validation and store acceptance/rejection.
- Node tests: schema 2 research renderer, tab routing, positions/orders/health renderers
  with fake DOM, `textContent`-only rendering.
- Route tests: `/dashboard`, `/dashboard-legacy`, `/dashboard-app.js` require auth.
- Manual verification: real batch run, publish, restart (with user consent), headless
  browser smoke check through the public URL.

## Rollout

1. Implement on a worktree branch; full microcap test suite green.
2. Push to `origin/master` after a secret scan.
3. Hand-apply the route changes to the live dirty `signal_server.py`; replace live
   `dashboard.html` after saving it as `dashboard_legacy.html`.
4. Run selection and batch with private env, publish schema 2 snapshot.
5. Restart `trading-bot.service` only with user confirmation; verify via public URL.
