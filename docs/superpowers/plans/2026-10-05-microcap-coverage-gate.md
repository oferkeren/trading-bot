# Pooled Micro-cap Coverage Gate Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Produce a bounded, reproducible, read-only feasibility report for a dated current-and-delisted micro-cap universe and historical IBKR quotes/bars/news plus optional Marketaux news, without claiming calibrated probabilities.

**Architecture:** Keep a verified, point-in-time roster contract separate from broker and news-provider adapters. Probe small predeclared issuer/date windows and aggregate availability, errors and source provenance into a fail-closed JSON report; do not import the existing trading scanner or order engine. Stop at a coverage decision: the pooled replay/model is a *second plan*, written only after actual roster and provider capabilities are known.

**Tech Stack:** Python 3.10, existing `ibapi` in `venv`, standard-library `unittest`, `urllib`, `datetime`, `json`; existing `microcap_history.CoverageError`/`parse_utc`; existing `news_context.py` only as a reference for IBKR callback and request patterns. No new dependency. No trading account orders or broker order methods.

---

## File map and guardrails

- Create `microcap_roster.py`, `test_microcap_roster.py`: validate an externally sourced dated issuer/ticker membership manifest. Never silently infer historical membership from today's ticker.
- Create `microcap_ibkr_probe.py`, `test_microcap_ibkr_probe.py`: bounded callback-driven IBKR history/entitlement inspection for resolved contracts, bars, quotes and news; no market-order or account APIs.
- Create `microcap_marketaux_probe.py`, `test_microcap_marketaux_probe.py`: optional bounded timestamped article/coverage fetch with an environment-only token. Provider failure is explicit, not an empty news list.
- Create `microcap_coverage_probe.py`, `test_microcap_coverage_probe.py`: small-sample orchestrator, provider-independent coverage matrix, `NO_TRADE`/blocked reasons and CLI. `order_approval` is always `false`.
- Modify `OPERATIONS.md`: roster evidence, credentials by *name*, bounded invocation, probe limitations, and why coverage is not calibration.

Never edit dirty `scanner.py`, `news_context.py`, `strategy_engine.py`, `worker_core.py`, or any execution/risk files. Never check in issuer rosters that were obtained under third-party license, credentials, broker responses, or user-specific account data. Use a new distinct IBKR client ID configured in the environment, separate from the existing services; do not disconnect another client's session. Confirm request signatures/limits against the installed `ibapi` and provider docs before implementing; if an endpoint cannot supply historical quotes or timestamped news, record that as a coverage failure rather than substitute live data.

## Task 1: Dated issuer roster and source-evidence gate

**Files:** Create `microcap_roster.py`, `test_microcap_roster.py`.

- [ ] **Step 1: Write the failing test.** Define JSON input as an object with `source_url`, `retrieved_at`, `coverage_claim`, and `members` (each `issuer_id`, `symbol`, `company`, `valid_from`, `valid_to`, `listing_status`, `source_record_id`). `valid_to` is exclusive, or `null` if still listed. Each date has a timezone; `listing_status` is `listed` or `delisted`. Use this regression shape:

```python
import unittest
from microcap_history import CoverageError
from microcap_roster import eligible_members

class RosterTests(unittest.TestCase):
    def test_point_in_time_identity(self):
        roster = {"source_url": "https://example.org/issuer-directory",
                  "retrieved_at": "2026-10-05T00:00:00Z",
                  "coverage_claim": "historical-listed-and-delisted",
                  "members": [
                      {"issuer_id": "issuer-1", "symbol": "OLD", "company": "Example Co",
                       "valid_from": "2020-01-01T00:00:00Z", "valid_to": "2022-01-01T00:00:00Z",
                       "listing_status": "delisted", "source_record_id": "row-1"},
                      {"issuer_id": "issuer-1", "symbol": "NEW", "company": "Example Co",
                       "valid_from": "2022-01-01T00:00:00Z", "valid_to": None,
                       "listing_status": "listed", "source_record_id": "row-2"}]}
        self.assertEqual([m["symbol"] for m in eligible_members(roster, "2021-01-01T00:00:00Z")], ["OLD"])
        self.assertEqual([m["symbol"] for m in eligible_members(roster, "2023-01-01T00:00:00Z")], ["NEW"])

    def test_missing_roster_provenance_fails_closed(self):
        with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
            eligible_members({"members": []}, "2024-01-01T00:00:00Z")
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_roster -q`; expect missing-module failure.
- [ ] **Step 3: Implement** `eligible_members(roster: Mapping[str, object], at: str) -> list[dict[str, object]]` using `parse_utc`: require nonempty source URL, retrieval timestamp, the exact coverage claim, and at least one member. Validate all required member fields, non-overlapping validity intervals for the same issuer/symbol, `valid_from < valid_to` when bounded, and that one symbol does not map to two issuers on the same date. Reject duplicate `source_record_id`. Return records whose half-open interval contains `at`, retaining *all* provenance; an empty valid result is `ROSTER_NO_MEMBERS_AT_DATE`, never a proof the universe was empty. Do not trust `coverage_claim` alone as evidence of an actual source: Task 2 checks it externally.
- [ ] **Step 4: Add tests** for malformed timestamps, duplicated/overlapping identities, delisted record retention, unknown source, and absent membership; rerun `venv/bin/python -m unittest test_microcap_roster -q`, expect PASS.
- [ ] **Step 5: Investigate a real dated source** of listed **and delisted** US micro-cap issuers under usable terms. Record URL, publisher, as-of dates, how historical constituents/delistings are obtained, ticker-change mapping, known missing cohorts, and permitted local retention in a private session artifact (not the repository). The existing report cannot call a present-day listing "historical"; if no verifiable source exists, flag `ROSTER_UNVERIFIED`, continue only with clearly labeled *pilot* provider probes, and block any pooled-calibration claim. No speculative fixture is passed as a real source. `coverage_claim` in JSON is an assertion to audit against this independent evidence, never self-verifying.
- [ ] **Step 6: Commit** `microcap_roster.py` and `test_microcap_roster.py` with the required `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer.

## Task 2: Read-only, bounded IBKR historical data probe

**Files:** Create `microcap_ibkr_probe.py`, `test_microcap_ibkr_probe.py`.

- [ ] **Step 1: Write failing offline tests** with a fake `EClient`/`EWrapper` callback driver (no actual socket): contract lookup resolves a unique `conId`; ambiguous/unknown/delisted contracts produce `CONTRACT_UNRESOLVED`; bars and bid/ask are *separate* timestamped coverage results; only provider IDs returned by IBKR are requested for news; a no-news response is `NEWS_COVERAGE_UNVERIFIED`; an error, disconnect, timeout, or pacing response is an unavailable reason, not an empty-success result. Assert the driver never calls `placeOrder`, `reqPositions`, `reqOpenOrders`, or trading endpoints. Example:

```python
import unittest
from microcap_ibkr_probe import summarize_observations

class IBKRProbeTests(unittest.TestCase):
    def test_missing_quotes_blocks_even_with_bars(self):
        result = summarize_observations(
            bars=[{"t": "2024-05-15T14:30:00Z"}], quotes=[], articles=[],
            provider_codes=["BRFG"], errors=[])
        self.assertEqual(result["quotes"]["status"], "unavailable")
        self.assertIn("QUOTE_COVERAGE_UNAVAILABLE", result["reasons"])
        self.assertEqual(result["news"]["status"], "unverified")
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_ibkr_probe -q`; expect missing-module failure.
- [ ] **Step 3: Implement** a pure `summarize_observations(*, bars, quotes, articles, provider_codes, errors)` that reports counts, first/last UTC timestamps, explicit missing/timeout/permission reasons and article/provider provenance. Never equate zero articles with verified negative news coverage; reject malformed or time-naive timestamps. Use `parse_utc`, and preserve coverage for bars even when quotes fail.
- [ ] **Step 4: Implement** `probe_ibkr(symbol, start, end, *, host, port, client_id, timeout_seconds=10)` with `ibapi` `EWrapper`/`EClient` callbacks and per-request IDs/events. Use unique resolved stock contract, `reqHistoricalData` (`TRADES`, `1 min`, plus `1 hour`/`1 day` sample windows), and a separate `BID_ASK` historical request or historical bid/ask ticks **only if the installed API and entitlement actually support them**. Obtain news provider codes via `reqNewsProviders`, then request bounded `reqHistoricalNews` for the resolved `conId`; record returned provider, article ID, and publication time. Limit each channel to a short predeclared window, use fixed timeout and serialized requests with a measured pacing pause, capture IBKR errors by request ID and code, disconnect only this new app in `finally`. If `conId` cannot be resolved, skip dependent requests with explicit reasons. Parse IBKR timestamps with the actual returned format/timezone; if ambiguous, reject rather than assume UTC. This adapter only issues historical-data, contract and news read requests; do not import or call order modules.
- [ ] **Step 5: Add callback tests** for `historicalDataEnd`, `historicalNewsEnd`, disconnect during request, permission errors and pacing errors. Run `venv/bin/python -m unittest test_microcap_ibkr_probe -q`, expect PASS. Document actual API signatures and response formats in `OPERATIONS.md` as observed, not assumed.
- [ ] **Step 6: Commit** this adapter/tests with the required co-author trailer.

## Task 3: Optional Marketaux news-depth probe

**Files:** Create `microcap_marketaux_probe.py`, `test_microcap_marketaux_probe.py`.

- [ ] **Step 1: Write failing offline tests** mocking `urlopen`: no token rejects without HTTP; a 401/403/429/5xx or malformed payload is `NEWS_PROVIDER_UNAVAILABLE` or `NEWS_RATE_LIMITED`; a valid article retains issuer/entity identity, UTC publication time, source and ID; a valid empty page reports `NEWS_COVERAGE_UNVERIFIED`, not "no catalyst." Do not assert free-plan quotas from marketing copy; measure actual limit responses.

```python
import os
import unittest
from unittest.mock import patch
from microcap_history import CoverageError
from microcap_marketaux_probe import MarketauxProbe

class MarketauxTests(unittest.TestCase):
    def test_no_token_rejects_request(self):
        with patch.dict(os.environ, {"MARKETAUX_API_TOKEN": ""}):
            with self.assertRaisesRegex(CoverageError, "MARKETAUX_TOKEN_MISSING"):
                MarketauxProbe.from_environment()
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_marketaux_probe -q`; expect missing-module failure.
- [ ] **Step 3: Implement** `MarketauxProbe.from_environment()` and `fetch_articles(symbol, start, end, *, max_pages=2)` after confirming the current published Marketaux endpoint, query limits, entity fields and pagination contract. Accept only `https://api.marketaux.com` and never follow an untrusted redirect with a token. Validate each article ID, publication timestamp, source and matching entity; bound pagination, timeouts and result count, track exact requested windows and returned coverage. Redact the API token from all errors/reports (including URLs and HTTP exceptions); never print request headers. On missing entitlement, quota or incomplete pages return an explicit unavailable error. Do not let this optional source repair missing IBKR quotes.
- [ ] **Step 4: Add tests** for mismatched entities, duplicate IDs, future timestamps, truncated pages, and token redaction. Run `venv/bin/python -m unittest test_microcap_marketaux_probe -q`, expect PASS.
- [ ] **Step 5: Commit** only this adapter/tests with the required co-author trailer.

## Task 4: Coverage decision and safe CLI

**Files:** Create `microcap_coverage_probe.py`, `test_microcap_coverage_probe.py`; modify `OPERATIONS.md`.

- [ ] **Step 1: Write failing pure-report tests** against `coverage_report(roster_status, observations, *, sample_manifest)`: an unverified roster, no quotes, unresolved delisted contract, missing publication times, or uncertain provider coverage produces `NO_TRADE`; even a coverage-passing probe has `order_approval: false`, `probabilities: unavailable`, and `model_calibrated: false`. Mixed observations report counts by issuer/date/session/provider and preserve errors.

```python
import unittest
from microcap_coverage_probe import coverage_report

class CoverageDecisionTests(unittest.TestCase):
    def test_quotes_unavailable_cannot_pass(self):
        result = coverage_report(
            {"status": "verified"}, [{"issuer_id": "a", "date": "2024-05-15",
               "bars": {"status": "observed"}, "quotes": {"status": "unavailable"},
               "news": {"status": "observed"}}],
            sample_manifest={"status": "predeclared", "issuer_ids": ["a"]})
        self.assertEqual(result["decision"], "NO_TRADE")
        self.assertFalse(result["order_approval"])
        self.assertFalse(result["model_calibrated"])
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_coverage_probe -q`; expect missing-module failure.
- [ ] **Step 3: Implement** `coverage_report` as a pure function with version, issuer/date/session/provider matrix, missing/unsupported fractions, timestamp precision, source records, sample manifest, `decision` and explicit blocking reasons. A coverage result is **not** a calibrated trading signal: even `PROBE_COVERAGE_OBSERVED` remains `NO_TRADE`, `order_approval: false`, `model_calibrated: false`, and unavailable probabilities. If data are sparse, describe which requirement failed; never emit probabilities.
- [ ] **Step 4: Implement CLI** requiring absolute external `--roster` and `--sample-manifest` JSON paths and UTC start/end; use `--observations` for offline fixture replay, or explicit `--fetch` with a dedicated `IB_PROBE_CLIENT_ID` plus bounded `--max-symbols`/`--max-days`. Default without `--fetch` never connects. Verify the roster's claim against independently collected evidence; if unverifiable, allow only explicit `--pilot --fetch` for a predeclared, clearly biased sample and hard-code `ROSTER_UNVERIFIED` in its report. Refuse a sample selected *after* viewing outcomes. Do not auto-run on the whole universe. Never connect during imports/tests. Use exit code 0 for valid `NO_TRADE` JSON, 2 for invalid input, 3 for provider/entitlement errors, with one redacted JSON stderr line; write outputs outside the repository. Disconnect on errors. Add mocked CLI tests to ensure default/no-`--fetch` never connects and `--pilot` cannot report calibrated status.
- [ ] **Step 5: Update `OPERATIONS.md`** with exact `--help`-verified invocation, environment variable names only, rate/window limits, roster provenance/point-in-time constraints, measured entitlement results *if actually probed* (otherwise label unverified), and the meaning of `PROBE_COVERAGE_OBSERVED`. Do not claim a provider guarantees historical completeness. Run `venv/bin/python -m unittest test_microcap_roster test_microcap_ibkr_probe test_microcap_marketaux_probe test_microcap_coverage_probe test_microcap_history test_microcap_rebounds test_microcap_research -q`; expect every offline test PASS. Inspect `git diff --check` and `git status`; commit only the probe files/docs with the required co-author trailer.

## Feasibility handoff (not automatic activation)

After the offline probe tests pass, run only the bounded read-only probe if the user makes the required IBKR session and optional Marketaux token available privately. Predeclare sampled dates and issuers **before** querying outcomes; record whether the roster actually covers delisted issuers, IBKR historical quotes and old news return timestamped data, and whether Marketaux's actual quota/depth can help. No token or broker account number appears in the report.

**Stop condition:** If the dated roster cannot be sourced, quotes are not entitled or deep enough for comparable outcomes, or neither news source provides adequately timestamped historical catalysts, publish `NO_TRADE` and a prospective-collection recommendation; do not write a pooled-probability implementation against invented history.

**If coverage passes:** Write a *separate* implementation plan for the pooled point-in-time scan/replay, active post-publication catalyst reaction, cross-source deduplication, issuer/episode clustering, purged 60/20/20 splits, 50 comparable train-plus-validation / 10-per-split independent episodes, frozen cost-aware model and untouched holdout. Preserve research-only output and the original 2026-10-05 spec. Real-data calibration is a later verification step, not a result of passing this probe.
