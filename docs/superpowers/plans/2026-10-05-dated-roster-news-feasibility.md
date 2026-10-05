# Dated Roster and News Feasibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the research-only coverage gate with bounded Massive dated-roster and Alpaca historical-news observations while enforcing a strict point-in-time $300M micro-cap evidence gate.

**Architecture:** Provider readers return independently tagged observations, never trade signals. A pure evidence gate checks availability-time-qualified share and price records; a thin read-only CLI combines these with the existing IBKR coverage report and always emits `NO_TRADE`. Stop at an explicit data-feasibility result rather than implementing a pooled model without a verified shares source.

**Tech Stack:** Python 3.10, standard-library `urllib`/`unittest`/`datetime`, existing `microcap_history.AlpacaHistory`/`CoverageError` and `microcap_coverage_probe.coverage_report`. Existing `venv/bin/python`; no new packages or execution APIs.

---

## File ownership and invariants

- Create `microcap_massive_roster.py`, `test_microcap_massive_roster.py`: read-only authenticated dated roster pagination and normalized identity/coverage evidence. Do **not** use Massive `market_cap` or `weighted_shares_outstanding`.
- Create `microcap_news_coverage.py`, `test_microcap_news_coverage.py`: Alpaca **news-only** adapter around existing `AlpacaHistory.fetch_news`; preserve timestamps, provider and ID, never infer completeness from missing news.
- Create `microcap_cap_evidence.py`, `test_microcap_cap_evidence.py`: pure <= $300M gate using then-known share classes, filing availability and raw price basis; default unavailable when no validated shares source exists.
- Create `microcap_source_probe.py`, `test_microcap_source_probe.py`: opt-in bounded source CLI and pure extension of the existing IBKR coverage report; no broker connections in this module.
- Modify `OPERATIONS.md`: exact bounded commands, evidence interpretation, environment-variable names only, Basic two-year plan limits, Marketaux's observed Cloudflare 403, and filing-date share blocker.

Use a new worktree: main `master` has unrelated uncommitted live-bot changes. No secrets in tests, command arguments, reports or git. The previously pasted provider keys must be rotated privately. An actual API response is not automatically complete or correctly timestamped. No probability, trade approval, scanner modification, or order path.

## Task 1: Massive dated roster reader

**Files:** Create `microcap_massive_roster.py`, `test_microcap_massive_roster.py`.

- [ ] **Step 1: Write failing offline tests** using mocked `urlopen` for `fetch_snapshot("2025-10-01", active=True, ticker="SORA", max_pages=2)` and `active=False`. Assert outgoing `market=stocks`, `locale=us`, `date=2025-10-01`, explicit `active`, page cap and key present only in HTTPS request, **never** returned. Fake one result containing `ticker`, `active`, `name`, `market`, `locale`, `cik`, `composite_figi`, `primary_exchange`, `delisted_utc`. Also test `next_url` pagination, foreign host, repeated URL, conflicting identities, HTTP 401/403/429, missing/invalid response fields, and the bound when a page still has `next_url`.

```python
import os
import unittest
from unittest.mock import patch
from microcap_history import CoverageError
from microcap_massive_roster import MassiveRoster

class MassiveRosterTests(unittest.TestCase):
    def test_missing_key_fails_before_http(self):
        with patch.dict(os.environ, {"MASSIVE_API_KEY": ""}):
            with self.assertRaisesRegex(CoverageError, "MASSIVE_KEY_MISSING"):
                MassiveRoster.from_environment()
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_massive_roster -q`; expect module-import failure.
- [ ] **Step 3: Implement** `MassiveRoster.from_environment()` and `fetch_snapshot(date: str, *, active: bool, ticker: str | None = None, max_pages: int = 2) -> dict[str, object]`. Validate ISO calendar date; require `1 <= max_pages <= 10`. GET `https://api.massive.com/v3/reference/tickers` with documented filters and `limit<=1000`. For every `next_url`, verify scheme/host/path, reject loops, reattach key privately, never follow redirects away from the host. Reject expired/truncated/malformed responses with `ROSTER_COVERAGE_UNVERIFIED`, or HTTP 429 with `ROSTER_RATE_LIMITED`; do not return a partial success list. Keep bounded timeouts and provider-defined pacing (free account is documented as rate-limited, but measure actual `429`/retry metadata). Normalize records as `{ticker, active, issuer identifiers, primary_exchange, type, delisted_utc, snapshot_date, fetched_at, source, identity_status}`; missing CIK **and** FIGI or ambiguous mapping means `identity_status=unverified`, not an invented issuer ID. Discard provider `market_cap`/shares even if present. Treat `active=false` as an inactive control, not a past active-membership assertion.
- [ ] **Step 4: Add regression tests** for `ticker` reuse (one ticker/two issuers), false `active` response under true request, symbol/date consistency and captured output/error token redaction. Run `venv/bin/python -m unittest test_microcap_massive_roster -q`; expect PASS.
- [ ] **Step 5: Commit** just these two files with `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## Task 2: Alpaca news-only observation

**Files:** Create `microcap_news_coverage.py`, `test_microcap_news_coverage.py`.

- [ ] **Step 1: Write failing tests** with a fake `AlpacaHistory.fetch_news`: one SORA article retains ID, provider, headline, symbol tags, published UTC, fetched UTC, requested half-open window; zero articles returns explicit `NEWS_COVERAGE_UNVERIFIED`; article after decision cannot qualify; wrong-company headline/tags cannot be promoted to a catalyst; exception stays an unavailable reason, not a zero count.

```python
import unittest
from microcap_news_coverage import observed_news

class AlpacaNewsCoverageTests(unittest.TestCase):
    def test_zero_articles_does_not_prove_absence(self):
        class Client:
            def fetch_news(self, symbols, start, end):
                return []
        evidence = observed_news(Client(), "SORA", "2025-05-28T00:00:00Z",
                                 "2025-05-29T00:00:00Z")
        self.assertEqual(evidence["status"], "NEWS_COVERAGE_UNVERIFIED")
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_news_coverage -q`; expect module-import failure.
- [ ] **Step 3: Implement** `observed_news(client: AlpacaHistory, symbol: str, start: str, end: str) -> dict[str, object]` using existing `fetch_news([symbol], start, end)`; `parse_utc` validates `start <= published < end` with inclusive start/exclusive end, and `published <= fetched_at`. Count unique IDs; conflicting duplicate IDs, unknown publication time or mismatched tags fail explicitly. Return article evidence with source and IDs but mark `completeness="UNVERIFIED_BY_PROVIDER"` even when articles exist. No headline relevance or post-publication price/volume inference in this feasibility phase.
- [ ] **Step 4: Add tests** for duplicates, invalid timezone, half-open end, and provider error; rerun targeted tests and ensure PASS.
- [ ] **Step 5: Commit** only adapter/tests with co-author trailer.

## Task 3: Strict as-of capitalization evidence

**Files:** Create `microcap_cap_evidence.py`, `test_microcap_cap_evidence.py`.

- [ ] **Step 1: Write failing pure tests**: $300M inclusive passes only with then-known raw price and complete same-issuer outstanding share classes; $300M plus one cent is not micro-cap; missing `filed_at`/`available_at`, future filing, stale/mismatched split basis, partial class coverage, reused ticker or a value from Massive ticker details returns `MARKET_CAP_UNVERIFIED`. Do not equate missing shares to zero.

```python
import unittest
from microcap_cap_evidence import classify_microcap

class CapitalizationTests(unittest.TestCase):
    def test_future_filing_cannot_classify_a_past_event(self):
        result = classify_microcap(
            issuer_id="issuer-1", decision_at="2025-05-28T15:00:00Z",
            price={"raw_close": 2.0, "known_at": "2025-05-28T14:59:00Z",
                   "basis": "raw", "issuer_id": "issuer-1", "class_id": "common"},
            shares=[{"count": 100_000_000, "issuer_id": "issuer-1",
                     "class_id": "common",
                     "filed_at": "2025-05-29T00:00:00Z",
                     "available_at": "2025-05-29T00:00:00Z",
                     "basis": "raw", "source": "filing"}],
            classes_complete=True, corporate_actions_verified=True,
            source_verified=True)
        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_cap_evidence -q`; expect module-import failure.
- [ ] **Step 3: Implement** `classify_microcap(*, issuer_id: str, decision_at: str, price: Mapping[str, object] | None, shares: Sequence[Mapping[str, object]] | None, classes_complete: bool, corporate_actions_verified: bool, source_verified: bool = False) -> dict[str, object]`. `source_verified` is an external audited-source witness, **not** an untrusted JSON field; the real CLI never sets it true because no shares source is wired in this plan. Require exactly one complete common share class with matching class ID and raw, unadjusted price/share basis (multi-class issuers need separately verified class prices and remain unavailable here). Require `known_at <= decision_at` and at most five minutes old, `filed_at <= available_at <= decision_at`, verified issuer ID, corporate actions and finite strictly positive count/price; calculate `raw_close * count` and classify `<= 300_000_000`. Return evidence timestamps and raw market cap only when all prerequisites pass; otherwise `{"status":"MARKET_CAP_UNVERIFIED","reason":...}`. Treat Massive ticker-details shares as **disallowed evidence** regardless of returned date.
- [ ] **Step 4: Add tests** for the synthetic witness-gated exact $300M boundary, multiple share classes, negative/NaN values, future availability despite old report-period end, raw/split basis mismatch, and a close older than five minutes. Run targeted tests; expect PASS.
- [ ] **Step 5: Commit** module/tests with co-author trailer.

## Task 4: Integrated fail-closed feasibility report and operations

**Files:** Create `microcap_source_probe.py`, `test_microcap_source_probe.py`; modify `OPERATIONS.md`. Keep `microcap_coverage_probe.py`'s current behavior intact.

- [ ] **Step 1: Write failing tests** for `extend_coverage(base_report, *, roster, alpaca_news, cap_evidence)`. Start from a real shape returned by existing `coverage_report`; ensure `decision` is always `NO_TRADE`, `order_approval`/`model_calibrated` remain false, and `target_probabilities` remains unavailable even with complete paginated Massive roster and positive Alpaca articles. Missing shares/invalid roster adds `MARKET_CAP_UNVERIFIED`/`ROSTER_COVERAGE_UNVERIFIED`; provider denial adds explicit unavailable reasons; report retains minute/hour/day IBKR matrix and separates Massive dated active/inactive and Alpaca article counts. Reject a base report containing `decision != NO_TRADE` rather than silently repairing it.

```python
import unittest
from microcap_coverage_probe import coverage_report
from microcap_source_probe import extend_coverage

class CombinedCoverageTests(unittest.TestCase):
    def test_sources_cannot_override_no_trade(self):
        manifest = {"status": "predeclared", "issuer_ids": ["issuer-1"],
                    "dates": ["2025-05-28"]}
        base = coverage_report({"status": "unverified"}, [], sample_manifest=manifest)
        base["request_window"] = {"start_utc": "2025-05-28T00:00:00Z",
                                  "end_utc": "2025-05-29T00:00:00Z"}
        report = extend_coverage(base, roster=None, alpaca_news=None, cap_evidence=None)
        self.assertEqual(report["decision"], "NO_TRADE")
        self.assertIn("MARKET_CAP_UNVERIFIED", report["reasons"])
```

- [ ] **Step 2: Run** `venv/bin/python -m unittest test_microcap_source_probe -q`; expect module-import failure.
- [ ] **Step 3: Implement** pure `extend_coverage` with fixed output schema and explicit source statuses. Reject a forged base report unless it has `decision="NO_TRADE"`, `order_approval=False`, `model_calibrated=False`, unavailable probabilities, a version, and matching predeclared issuer/date/window; a self-declared `"verified"` roster in the base report is not independent evidence. CLI accepts absolute predeclared `--base-report`, `--sample-manifest`, `--start`, `--end`; default offline reads externally saved evidence JSON, `--fetch` explicitly permits the *new* read-only Massive/Alpaca calls, bounded to one date/issuer in the first pilot with `--max-pages <= 2`. Check manifest and base-report issuer/date/window consistency **before** network requests. The roster probe uses Massive `active=true` **and** `active=false`, refuses incomplete pagination and flags 2-year entitlement limits; Alpaca uses existing `AlpacaHistory.from_environment()` only for news. Secrets are environment-only, no token in errors/URLs saved to report, external files absolute; do not import order/scanner/worker modules. Provider failures exit 3 with sanitized JSON stderr; invalid inputs exit 2; valid `NO_TRADE` JSON exits 0. A missing actual filing-date-qualified shares series always forces `MARKET_CAP_UNVERIFIED` even if the Massive roster looks complete.
- [ ] **Step 4: Add tests** for no HTTP in offline mode, bounded fetch with fake providers, mismatch manifest, incomplete roster page, empty Alpaca news, 403 access error, bad base report, leakage of configured tokens in user-supplied evidence, and exact `NO_TRADE` JSON shape. Run `venv/bin/python -m unittest test_microcap_massive_roster test_microcap_news_coverage test_microcap_cap_evidence test_microcap_source_probe test_microcap_roster test_microcap_ibkr_probe test_microcap_marketaux_probe test_microcap_coverage_probe test_microcap_history test_microcap_rebounds test_microcap_research -q`; expect all PASS. Confirm `git diff --check`.
- [ ] **Step 5: Update `OPERATIONS.md`** with `--help`-verified offline and capped real commands using only variable names (`MASSIVE_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`); record that the single Massive roster/date query and two Alpaca SORA windows succeeded, Marketaux returned Cloudflare 403, source completeness and filed shares unverified. Commit only new files/tests/docs with co-author trailer.

## Gate after implementation

Run a bounded real source pilot only with privately configured **rotated** credentials and an external predeclared sample. Do not store licensed roster data or keys in git. Report actual pagination, dated symbol/identifier gaps and news observations, never a strategy probability. Before a pooled-model plan can be written, obtain an availability-time-qualified shares source and validate the dated roster across many listed and subsequently delisted names. If either remains unverified, stop at `NO_TRADE`; Phase 2 and Phase 3 stay blocked.
