# Micro-cap Batch Coverage Pilot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Select up to 20 recent micro-cap runner days mechanically, run each through the existing IBKR / Massive+Alpaca / SEC probes, and publish a schema 2 readiness snapshot with per-source coverage counts and one row per sample.

**Architecture:** A shared strict schema module (`microcap_batch_schema.py`) defines manifests, batch reports and schema 2 snapshots. `microcap_batch_select.py` builds a hashed manifest from Massive grouped daily bars, Massive ticker details and SEC `company_tickers.json`. `microcap_batch_run.py` drives the existing CLIs in subprocesses per sample and aggregates rows. `microcap_readiness.py` gains `--batch-report`; `microcap_readiness_store.py` accepts schema 1 and 2.

**Tech Stack:** Python 3.10 stdlib (`urllib`, `subprocess`, `zoneinfo`, `hashlib`), `unittest`. Run tests with `/home/oferke/trading-bot/venv/bin/python -m unittest <module>` (pytest is not installed).

**Spec:** `docs/superpowers/specs/2026-10-05-microcap-multi-sample-pilot-design.md` (sections 1–3).

**Global rules for every task:**
- Research only. Every output carries `decision: "NO_TRADE"`, `order_approval: false`, `model_calibrated: false`, `target_probabilities: "unavailable"`.
- Never put credential values in arguments, outputs, test fixtures, or error text. No credential-shaped strings (`sk_live_…`, `AKIA…`, `ghp_…`) in tests.
- All file inputs/outputs are absolute paths outside the repository (reuse `microcap_readiness._external`).
- Tests use no network. HTTP is faked by patching the module-level `urlopen`.
- Run from the worktree root. `PY=/home/oferke/trading-bot/venv/bin/python`.

## File structure

| File | Responsibility |
|---|---|
| `microcap_batch_schema.py` (new) | Constants, session window, manifest hash, validators for manifest / sample row / batch report / schema 2 snapshot, schema 2 projection |
| `test_microcap_batch_schema.py` (new) | Tests for the above |
| `microcap_batch_select.py` (new) | Screen, rate limiter, Massive and SEC clients, `select_batch`, CLI |
| `test_microcap_batch_select.py` (new) | Tests with fake providers and patched `urlopen` |
| `microcap_batch_run.py` (new) | Per-sample stages via subprocess, summarize, batch loop, resume, pacing, CLI |
| `test_microcap_batch_run.py` (new) | Tests with a fake stage runner |
| `microcap_readiness.py` (modify) | `publish_batch_readiness`, `--batch-report` CLI option |
| `microcap_readiness_store.py` (modify) | Dispatch schema 2 validation |
| `test_microcap_readiness.py`, `test_microcap_readiness_store.py` (modify) | Schema 2 publish/read tests |
| `OPERATIONS.md` (modify) | Section 41: batch pilot usage |

---

### Task 1: Batch schema module

**Files:**
- Create: `microcap_batch_schema.py`
- Test: `test_microcap_batch_schema.py`

- [ ] **Step 1: Write the failing tests**

Create `test_microcap_batch_schema.py`:

```python
import copy
import unittest
from datetime import date, datetime, timezone

from microcap_batch_schema import (
    BIAS, DEFAULT_RULE, coverage_from_rows, manifest_hash, project_batch_snapshot,
    session_window, validate_batch_report, validate_manifest, validate_row,
    validate_snapshot_v2,
)
from microcap_history import CoverageError


NOW = datetime(2026, 10, 5, 12, 0, 0, tzinfo=timezone.utc)


def sample(cik="0000000001", symbol="SORA", day="2025-05-28", move=52.3):
    start, end = session_window(date.fromisoformat(day))
    return {"issuer_id": cik, "symbol": symbol, "company": "Sora Example",
            "date": day, "move_pct": move, "start_utc": start, "end_utc": end}


def manifest(samples=None):
    body = {
        "schema_version": 1, "kind": "microcap_batch_manifest",
        "created_at": "2026-10-05T10:00:00Z", "as_of": "2025-05-28",
        "rule": dict(DEFAULT_RULE),
        "trading_days": [{"date": "2025-05-28", "request_id": "req-1"}],
        "candidates": [{"date": "2025-05-28", "ticker": "SORA", "move_pct": 52.3,
                        "outcome": "SELECTED"}],
        "samples": samples if samples is not None else [sample()],
        "bias": list(BIAS),
    }
    body["sha256"] = manifest_hash(body)
    return body


def row(**changes):
    value = {"date": "2025-05-28", "symbol": "SORA", "issuer_id": "0000000001",
             "move_pct": 52.3, "ibkr_minute": True, "ibkr_quotes": True,
             "roster": "DATED_ROSTER_OBSERVED", "news_count": 2,
             "sec_observations": 1, "stage_errors": ["ibkr:PROVIDER_ERROR"]}
    value.update(changes)
    return value


def batch_report(rows=None):
    rows = rows if rows is not None else [row()]
    return {
        "kind": "microcap_batch_report", "schema_version": 1,
        "generated_at": "2026-10-05T11:00:00Z", "decision": "NO_TRADE",
        "order_approval": False, "model_calibrated": False,
        "target_probabilities": "unavailable",
        "batch": {"as_of": "2025-05-28", "manifest_sha256": "a" * 64,
                  "sample_count": len(rows), "rule": dict(DEFAULT_RULE),
                  "bias": list(BIAS)},
        "coverage": coverage_from_rows(rows), "samples": rows,
        "blockers": ["MARKET_CAP_UNVERIFIED", "RESEARCH_ONLY_NOT_CALIBRATED",
                     "ROSTER_COVERAGE_UNVERIFIED"],
    }


class SessionWindowTests(unittest.TestCase):
    def test_regular_session_converts_daylight_and_standard_time(self):
        self.assertEqual(session_window(date(2025, 5, 28)),
                         ("2025-05-28T13:30:00Z", "2025-05-28T20:00:00Z"))
        self.assertEqual(session_window(date(2025, 12, 1)),
                         ("2025-12-01T14:30:00Z", "2025-12-01T21:00:00Z"))


class ManifestTests(unittest.TestCase):
    def test_valid_manifest_passes(self):
        validate_manifest(manifest())

    def test_edited_manifest_is_rejected_by_hash(self):
        value = manifest()
        value["samples"][0]["symbol"] = "EDIT"
        with self.assertRaises(CoverageError):
            validate_manifest(value)

    def test_structural_violations_are_rejected(self):
        two = [sample(), sample(symbol="ABCD")]
        for value in (
            manifest(samples=[]),
            manifest(samples=two),  # duplicate issuer
            manifest(samples=[dict(sample(), start_utc="2025-05-28T13:00:00Z")]),
            manifest(samples=[dict(sample(), symbol="BRK.A")]),
            manifest(samples=[dict(sample(), issuer_id="123")]),
        ):
            with self.subTest(value=value["samples"]), self.assertRaises(CoverageError):
                validate_manifest(value)

    def test_bias_and_kind_are_fixed(self):
        for change in (lambda v: v.update(bias=["RUNNER_SCREEN_SELECTED"]),
                       lambda v: v.update(kind="other"),
                       lambda v: v.update(extra=True)):
            value = manifest()
            change(value)
            value["sha256"] = manifest_hash(value)
            with self.assertRaises(CoverageError):
                validate_manifest(value)


class RowAndReportTests(unittest.TestCase):
    def test_valid_row_and_coverage(self):
        validate_row(row())
        rows = [row(), row(issuer_id="0000000002", symbol="ABCD", ibkr_minute=False,
                           news_count=None, roster="MISSING", sec_observations=0,
                           stage_errors=["ibkr:TIMEOUT", "source:SKIPPED"])]
        self.assertEqual(coverage_from_rows(rows), {
            "ibkr_minute": {"observed": 1, "total": 2},
            "ibkr_quotes": {"observed": 2, "total": 2},
            "roster_dated": {"observed": 1, "total": 2},
            "news_found": {"observed": 1, "total": 2},
            "sec_shares": {"observed": 1, "total": 2},
        })

    def test_invalid_rows_are_rejected(self):
        for value in (row(stage_errors=["ibkr:OK"]), row(roster="provider text"),
                      row(news_count=True), row(sec_observations=3),
                      row(symbol="<b>"), row(move_pct=float("nan")),
                      row(stage_errors=["source:SKIPPED", "ibkr:TIMEOUT"]),
                      dict(row(), raw="x")):
            with self.subTest(value=value), self.assertRaises(CoverageError):
                validate_row(value)

    def test_report_coverage_must_match_rows(self):
        validate_batch_report(batch_report())
        value = batch_report()
        value["coverage"]["news_found"]["observed"] = 0
        with self.assertRaises(CoverageError):
            validate_batch_report(value)

    def test_report_rejects_approval_unknown_blocker_and_duplicates(self):
        for change in (lambda v: v.update(order_approval=True),
                       lambda v: v["blockers"].append("ZZZ_PROVIDER_TEXT"),
                       lambda v: v["blockers"].remove("MARKET_CAP_UNVERIFIED"),
                       lambda v: v.update(samples=[row(), row()])):
            value = batch_report()
            change(value)
            with self.assertRaises(CoverageError):
                validate_batch_report(value)


class SnapshotTests(unittest.TestCase):
    def test_projection_produces_valid_schema_2_snapshot(self):
        snapshot = project_batch_snapshot(batch_report(), now=NOW)
        self.assertEqual(snapshot["schema_version"], 2)
        self.assertEqual(snapshot["generated_at"], "2026-10-05T12:00:00Z")
        self.assertEqual(snapshot["decision"], "NO_TRADE")
        self.assertNotIn("kind", snapshot)
        self.assertEqual(validate_snapshot_v2(snapshot), NOW)

    def test_snapshot_validator_rejects_forged_fields(self):
        base = project_batch_snapshot(batch_report(), now=NOW)
        for change in (lambda v: v.update(schema_version=1),
                       lambda v: v.update(model_calibrated=True),
                       lambda v: v["batch"].update(sample_count=5),
                       lambda v: v.update(raw_provider_text="private")):
            value = copy.deepcopy(base)
            change(value)
            with self.assertRaises(CoverageError):
                validate_snapshot_v2(value)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_batch_schema`
Expected: `ModuleNotFoundError: No module named 'microcap_batch_schema'`

- [ ] **Step 3: Implement `microcap_batch_schema.py`**

```python
"""Strict schemas for micro-cap batch manifests, batch reports and schema 2 snapshots."""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
from collections.abc import Mapping
from datetime import date, datetime, time, timezone
from zoneinfo import ZoneInfo

from microcap_history import CoverageError
from microcap_readiness import (
    _CREDENTIAL_ENV, _REPORT_REASONS, _ROSTER_STATUSES, _SEC_BLOCKERS, _is_count,
    _utc_timestamp,
)


BIAS = ("RUNNER_SCREEN_SELECTED", "SELECTION_USES_SAME_DAY_OUTCOME", "UNVERIFIED_PILOT")
SYMBOL_PATTERN = "[A-Z]{1,6}"
DEFAULT_RULE = {
    "days": 10, "per_day": 2, "min_open": 1.0, "max_open": 20.0,
    "min_volume": 1_000_000, "min_move": 0.30, "max_candidates_per_day": 10,
    "symbol_pattern": SYMBOL_PATTERN,
}
OUTCOMES = frozenset({
    "SELECTED", "NOT_COMMON_STOCK", "NO_SEC_CIK", "DUPLICATE_ISSUER", "PROVIDER_ERROR",
})
STAGES = ("ibkr", "source", "sec")
STAGE_STATUSES = ("OK", "PROVIDER_ERROR", "INVALID", "TIMEOUT", "SKIPPED")
STAGE_ERRORS = frozenset(
    f"{stage}:{status}" for stage in STAGES for status in STAGE_STATUSES if status != "OK"
)
COVERAGE_KEYS = ("ibkr_minute", "ibkr_quotes", "roster_dated", "news_found", "sec_shares")
REQUIRED_BLOCKERS = frozenset({
    "MARKET_CAP_UNVERIFIED", "ROSTER_COVERAGE_UNVERIFIED", "RESEARCH_ONLY_NOT_CALIBRATED",
})
MAX_SAMPLES = 20
MAX_CALENDAR_DAYS = 21

_NEW_YORK = ZoneInfo("America/New_York")
_CIK = re.compile(r"[0-9]{10}\Z")
_SYMBOL = re.compile(SYMBOL_PATTERN + r"\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
_FIXED = {"decision": "NO_TRADE", "order_approval": False, "model_calibrated": False,
          "target_probabilities": "unavailable"}
_MANIFEST_KEYS = {"schema_version", "kind", "created_at", "as_of", "rule", "trading_days",
                  "candidates", "samples", "bias", "sha256"}
_SAMPLE_KEYS = {"issuer_id", "symbol", "company", "date", "move_pct", "start_utc", "end_utc"}
_ROW_KEYS = {"date", "symbol", "issuer_id", "move_pct", "ibkr_minute", "ibkr_quotes",
             "roster", "news_count", "sec_observations", "stage_errors"}
_BODY_KEYS = {"generated_at", "decision", "order_approval", "model_calibrated",
              "target_probabilities", "batch", "coverage", "samples", "blockers"}
_BATCH_KEYS = {"as_of", "manifest_sha256", "sample_count", "rule", "bias"}


def _invalid() -> CoverageError:
    return CoverageError("INPUT_INVALID")


def _require(condition: bool) -> None:
    if not condition:
        raise _invalid()


def _keys(value: object, expected: set[str]) -> bool:
    return type(value) is dict and value.keys() == expected


def _number(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _iso_date(value: object) -> date:
    _require(isinstance(value, str))
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise _invalid() from None
    _require(parsed.isoformat() == value)
    return parsed


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def session_window(day: date) -> tuple[str, str]:
    """Regular US session 09:30-16:00 America/New_York as UTC strings."""
    start = datetime.combine(day, time(9, 30), _NEW_YORK)
    end = datetime.combine(day, time(16, 0), _NEW_YORK)
    return _utc_text(start), _utc_text(end)


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def manifest_hash(manifest: Mapping) -> str:
    body = {key: value for key, value in manifest.items() if key != "sha256"}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


def validate_rule(rule: object) -> None:
    _require(_keys(rule, set(DEFAULT_RULE)))
    _require(_is_count(rule["days"], minimum=1) and rule["days"] <= MAX_SAMPLES)
    _require(_is_count(rule["per_day"], minimum=1) and rule["per_day"] <= 5)
    _require(_is_count(rule["min_volume"], minimum=0))
    _require(_is_count(rule["max_candidates_per_day"], minimum=1)
             and rule["max_candidates_per_day"] <= 50)
    for key in ("min_open", "max_open", "min_move"):
        _require(_number(rule[key]) and rule[key] >= 0)
    _require(rule["min_open"] <= rule["max_open"])
    _require(rule["symbol_pattern"] == SYMBOL_PATTERN)


def _validate_symbol(value: object) -> None:
    _require(isinstance(value, str) and bool(_SYMBOL.fullmatch(value)))


def _validate_cik(value: object) -> None:
    _require(isinstance(value, str) and bool(_CIK.fullmatch(value)))


def _validate_move(value: object) -> None:
    _require(_number(value) and 0 <= value <= 100_000)


def validate_manifest(manifest: object) -> None:
    _require(_keys(manifest, _MANIFEST_KEYS))
    _require(manifest["schema_version"] == 1 and type(manifest["schema_version"]) is int)
    _require(manifest["kind"] == "microcap_batch_manifest")
    _utc_timestamp(manifest["created_at"])
    _iso_date(manifest["as_of"])
    validate_rule(manifest["rule"])
    _require(manifest["bias"] == list(BIAS))
    _require(isinstance(manifest["sha256"], str) and bool(_SHA256.fullmatch(manifest["sha256"])))
    _require(manifest["sha256"] == manifest_hash(manifest))
    days = manifest["trading_days"]
    _require(isinstance(days, list) and len(days) <= manifest["rule"]["days"])
    for entry in days:
        _require(_keys(entry, {"date", "request_id"}))
        _iso_date(entry["date"])
        _require(isinstance(entry["request_id"], str)
                 and bool(_REQUEST_ID.fullmatch(entry["request_id"])))
    candidates = manifest["candidates"]
    _require(isinstance(candidates, list)
             and len(candidates) <= MAX_CALENDAR_DAYS * manifest["rule"]["max_candidates_per_day"])
    for entry in candidates:
        _require(_keys(entry, {"date", "ticker", "move_pct", "outcome"}))
        _iso_date(entry["date"])
        _validate_symbol(entry["ticker"])
        _validate_move(entry["move_pct"])
        _require(entry["outcome"] in OUTCOMES)
    samples = manifest["samples"]
    _require(isinstance(samples, list)
             and 1 <= len(samples) <= min(MAX_SAMPLES,
                                          manifest["rule"]["days"] * manifest["rule"]["per_day"]))
    issuers = set()
    for entry in samples:
        _require(_keys(entry, _SAMPLE_KEYS))
        _validate_cik(entry["issuer_id"])
        _require(entry["issuer_id"] not in issuers)
        issuers.add(entry["issuer_id"])
        _validate_symbol(entry["symbol"])
        _require(isinstance(entry["company"], str) and 0 < len(entry["company"]) <= 120
                 and entry["company"].isprintable())
        day = _iso_date(entry["date"])
        _validate_move(entry["move_pct"])
        _require((entry["start_utc"], entry["end_utc"]) == session_window(day))


def validate_row(row: object) -> None:
    _require(_keys(row, _ROW_KEYS))
    _iso_date(row["date"])
    _validate_symbol(row["symbol"])
    _validate_cik(row["issuer_id"])
    _validate_move(row["move_pct"])
    _require(type(row["ibkr_minute"]) is bool and type(row["ibkr_quotes"]) is bool)
    _require(isinstance(row["roster"], str) and row["roster"] in _ROSTER_STATUSES)
    _require(row["news_count"] is None or _is_count(row["news_count"]))
    _require(_is_count(row["sec_observations"]) and row["sec_observations"] <= 2)
    errors = row["stage_errors"]
    _require(isinstance(errors, list)
             and all(isinstance(item, str) and item in STAGE_ERRORS for item in errors)
             and errors == sorted(set(errors)))


def coverage_from_rows(rows: list[Mapping]) -> dict[str, dict[str, int]]:
    total = len(rows)
    observed = {
        "ibkr_minute": sum(1 for row in rows if row["ibkr_minute"]),
        "ibkr_quotes": sum(1 for row in rows if row["ibkr_quotes"]),
        "roster_dated": sum(1 for row in rows if row["roster"] == "DATED_ROSTER_OBSERVED"),
        "news_found": sum(1 for row in rows
                          if row["news_count"] is not None and row["news_count"] >= 1),
        "sec_shares": sum(1 for row in rows if row["sec_observations"] >= 1),
    }
    return {key: {"observed": observed[key], "total": total} for key in COVERAGE_KEYS}


def validate_blockers(value: object) -> list[str]:
    allowed = _REPORT_REASONS | _SEC_BLOCKERS
    _require(isinstance(value, list)
             and all(isinstance(item, str) and item in allowed for item in value)
             and value == sorted(set(value))
             and REQUIRED_BLOCKERS <= set(value))
    return value


def _validate_body(value: Mapping) -> datetime:
    for key, expected in _FIXED.items():
        _require(value[key] == expected and type(value[key]) is type(expected))
    generated = _utc_timestamp(value["generated_at"])
    batch = value["batch"]
    _require(_keys(batch, _BATCH_KEYS))
    _iso_date(batch["as_of"])
    _require(isinstance(batch["manifest_sha256"], str)
             and bool(_SHA256.fullmatch(batch["manifest_sha256"])))
    validate_rule(batch["rule"])
    _require(batch["bias"] == list(BIAS))
    rows = value["samples"]
    _require(isinstance(rows, list) and 1 <= len(rows) <= MAX_SAMPLES)
    keys = set()
    for row in rows:
        validate_row(row)
        key = (row["date"], row["issuer_id"])
        _require(key not in keys)
        keys.add(key)
    _require(_is_count(batch["sample_count"]) and batch["sample_count"] == len(rows))
    _require(value["coverage"] == coverage_from_rows(rows))
    validate_blockers(value["blockers"])
    serialized = canonical_json(value)
    _require(not any(secret and secret in serialized
                     for name in _CREDENTIAL_ENV
                     if (secret := os.environ.get(name, "").strip())))
    return generated


def validate_batch_report(value: object) -> datetime:
    _require(_keys(value, _BODY_KEYS | {"kind", "schema_version"}))
    _require(value["kind"] == "microcap_batch_report")
    _require(type(value["schema_version"]) is int and value["schema_version"] == 1)
    return _validate_body(value)


def validate_snapshot_v2(value: object) -> datetime:
    _require(_keys(value, _BODY_KEYS | {"schema_version"}))
    _require(type(value["schema_version"]) is int and value["schema_version"] == 2)
    return _validate_body(value)


def project_batch_snapshot(report: Mapping, *, now: datetime) -> dict[str, object]:
    _require(isinstance(now, datetime) and now.tzinfo is not None
             and now.utcoffset() is not None)
    validate_batch_report(report)
    snapshot = {key: copy.deepcopy(report[key]) for key in _BODY_KEYS}
    snapshot["schema_version"] = 2
    snapshot["generated_at"] = _utc_text(now)
    validate_snapshot_v2(snapshot)
    return snapshot
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_batch_schema`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add microcap_batch_schema.py test_microcap_batch_schema.py
git commit -m "Add strict schemas for micro-cap batch manifests, reports and snapshots

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```


---

### Task 2: Selection logic (screen, pacing, SEC tickers, batch selection)

**Files:**
- Create: `microcap_batch_select.py`
- Test: `test_microcap_batch_select.py`

Design notes:
- `screen(rows, rule)` keeps rows whose `T` matches `[A-Z]{1,6}`, `o` in `[min_open, max_open]`, `v >= min_volume`, and `h / o - 1 >= min_move`. It returns `(ticker, move_pct)` ranked by move descending, then ticker ascending. `move_pct = round((h / o - 1) * 100, 1)`. Rows with missing or non-finite numbers are skipped.
- `RateLimiter(limit=5, window=60.0, clock=time.monotonic, sleep=time.sleep)` blocks before a call until fewer than `limit` calls started in the last `window` seconds.
- `parse_sec_tickers(payload)` returns `{ticker: (cik10, title)}`. A ticker mapped to more than one CIK is dropped (ambiguous). Tickers that don't match the symbol pattern are ignored.
- `select_batch(provider, sec_map, *, as_of, rule, now)`: walks back from `as_of` (inclusive). Weekends are skipped without a provider call. It stops after `rule["days"]` trading days or 21 calendar days. For each candidate (at most `max_candidates_per_day`), checks run in this order: `NO_SEC_CIK`, `DUPLICATE_ISSUER`, then `provider.ticker_type`, which gives `NOT_COMMON_STOCK` or `PROVIDER_ERROR`. A `grouped` failure raises `SelectionError("PROVIDER_ERROR")` and aborts the run. Zero samples raises `SelectionError("NO_SAMPLES_SELECTED")`.
- Provider interface: `grouped(day) -> (request_id, results_list)`, where `results_list` is empty on non-trading days, and `ticker_type(ticker, day) -> str`. Both raise `ProviderError` on failure.

- [ ] **Step 1: Write the failing tests**

Create `test_microcap_batch_select.py`:

```python
import unittest
from datetime import date, datetime, timezone

from microcap_batch_schema import DEFAULT_RULE, validate_manifest
from microcap_batch_select import (
    ProviderError, RateLimiter, SelectionError, parse_sec_tickers, screen, select_batch,
)


NOW = datetime(2025, 6, 2, 12, 0, 0, tzinfo=timezone.utc)


def bar(ticker, o, h, v=2_000_000):
    return {"T": ticker, "o": o, "h": h, "l": o, "c": h, "v": v}


class FakeProvider:
    def __init__(self, days, types=None, failing_types=()):
        self.days = days
        self.types = types or {}
        self.failing_types = set(failing_types)
        self.calls = []

    def grouped(self, day):
        self.calls.append(("grouped", day.isoformat()))
        return f"req-{day.isoformat()}", self.days.get(day.isoformat(), [])

    def ticker_type(self, ticker, day):
        self.calls.append(("type", ticker))
        if ticker in self.failing_types:
            raise ProviderError("PROVIDER_ERROR")
        return self.types.get(ticker, "CS")


SEC = {
    "AAA": ("0000000001", "Alpha Inc"), "BBB": ("0000000002", "Beta Inc"),
    "CCC": ("0000000003", "Gamma Inc"), "DUP": ("0000000001", "Alpha Inc"),
}


class ScreenTests(unittest.TestCase):
    def test_filters_and_ranks(self):
        rows = [
            bar("AAA", 2.0, 3.0), bar("BBB", 2.0, 4.0), bar("CCC", 2.0, 3.0),
            bar("LOW", 0.5, 1.0), bar("HIGH", 25.0, 40.0), bar("THIN", 2.0, 4.0, v=10),
            bar("FLAT", 2.0, 2.4), bar("BRK.B", 2.0, 4.0), bar("TOOLONG", 2.0, 4.0),
            {"T": "BAD", "o": None, "h": 3.0, "v": 2_000_000},
            bar("ZERO", 0.0, 3.0),
        ]
        self.assertEqual(screen(rows, DEFAULT_RULE),
                         [("BBB", 100.0), ("AAA", 50.0), ("CCC", 50.0)])


class RateLimiterTests(unittest.TestCase):
    def test_sixth_call_waits_until_window_frees(self):
        clock = [0.0]
        sleeps = []

        def sleep(seconds):
            sleeps.append(seconds)
            clock[0] += seconds

        limiter = RateLimiter(limit=5, window=60.0, clock=lambda: clock[0], sleep=sleep)
        for _ in range(5):
            limiter.wait()
            clock[0] += 1.0
        limiter.wait()
        self.assertEqual(sleeps, [55.0])


class SecTickerTests(unittest.TestCase):
    def test_parses_pads_and_drops_ambiguous(self):
        payload = {
            "0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"},
            "1": {"cik_str": 2, "ticker": "XX", "title": "X One"},
            "2": {"cik_str": 3, "ticker": "XX", "title": "X Two"},
            "3": {"cik_str": 4, "ticker": "BRK-B", "title": "Class"},
            "4": {"cik_str": "5", "ticker": "STR", "title": "Bad cik"},
        }
        self.assertEqual(parse_sec_tickers(payload), {"AAA": ("0000000001", "Alpha Inc")})

    def test_rejects_non_object(self):
        with self.assertRaises(SelectionError):
            parse_sec_tickers([])


class SelectBatchTests(unittest.TestCase):
    def rule(self, **changes):
        value = dict(DEFAULT_RULE)
        value.update(changes)
        return value

    def test_selects_with_outcomes_and_valid_manifest(self):
        provider = FakeProvider(
            {"2025-05-30": [bar("AAA", 2.0, 4.0), bar("ZZZ", 2.0, 3.9),
                            bar("BBB", 2.0, 3.0), bar("CCC", 2.0, 2.9)],
             "2025-05-29": [bar("DUP", 2.0, 4.0), bar("CCC", 2.0, 3.0)]},
            types={"BBB": "WARRANT"},
        )
        manifest = select_batch(provider, SEC, as_of=date(2025, 5, 30),
                                rule=self.rule(days=2), now=NOW)
        validate_manifest(manifest)
        self.assertEqual([(s["date"], s["symbol"]) for s in manifest["samples"]],
                         [("2025-05-30", "AAA"), ("2025-05-30", "CCC")])
        outcomes = [(c["date"], c["ticker"], c["outcome"]) for c in manifest["candidates"]]
        self.assertEqual(outcomes, [
            ("2025-05-30", "AAA", "SELECTED"),
            ("2025-05-30", "ZZZ", "NO_SEC_CIK"),
            ("2025-05-30", "BBB", "NOT_COMMON_STOCK"),
            ("2025-05-30", "CCC", "SELECTED"),
            ("2025-05-29", "DUP", "DUPLICATE_ISSUER"),
            ("2025-05-29", "CCC", "DUPLICATE_ISSUER"),
        ])
        self.assertEqual(manifest["samples"][0]["start_utc"], "2025-05-30T13:30:00Z")
        self.assertEqual(manifest["samples"][0]["company"], "Alpha Inc")
        self.assertEqual(manifest["trading_days"],
                         [{"date": "2025-05-30", "request_id": "req-2025-05-30"},
                          {"date": "2025-05-29", "request_id": "req-2025-05-29"}])
        self.assertNotIn(("type", "ZZZ"), provider.calls)
        self.assertNotIn(("type", "DUP"), provider.calls)

    def test_weekends_skip_calls_and_holidays_do_not_count(self):
        provider = FakeProvider({"2025-05-30": [bar("AAA", 2.0, 4.0)]})
        manifest = select_batch(provider, SEC, as_of=date(2025, 6, 1),
                                rule=self.rule(days=1), now=NOW)
        grouped = [day for kind, day in provider.calls if kind == "grouped"]
        self.assertEqual(grouped, ["2025-05-30"])
        self.assertEqual(len(manifest["samples"]), 1)

    def test_stops_after_21_calendar_days(self):
        provider = FakeProvider({})
        with self.assertRaises(SelectionError) as raised:
            select_batch(provider, SEC, as_of=date(2025, 5, 30), rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "NO_SAMPLES_SELECTED")
        self.assertEqual(len([c for c in provider.calls if c[0] == "grouped"]), 15)

    def test_type_failure_is_recorded_and_grouped_failure_aborts(self):
        provider = FakeProvider({"2025-05-30": [bar("AAA", 2.0, 4.0), bar("BBB", 2.0, 3.0)]},
                                failing_types={"AAA"})
        manifest = select_batch(provider, SEC, as_of=date(2025, 5, 30),
                                rule=self.rule(days=1), now=NOW)
        self.assertEqual([c["outcome"] for c in manifest["candidates"]],
                         ["PROVIDER_ERROR", "SELECTED"])

        class Broken(FakeProvider):
            def grouped(self, day):
                raise ProviderError("PROVIDER_ERROR")

        with self.assertRaises(SelectionError) as raised:
            select_batch(Broken({}), SEC, as_of=date(2025, 5, 30), rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "PROVIDER_ERROR")

    def test_candidate_cap_per_day(self):
        rows = [bar(f"Q{chr(65 + i)}", 2.0, 4.0 - i * 0.01) for i in range(15)]
        provider = FakeProvider({"2025-05-30": rows})
        with self.assertRaises(SelectionError):
            select_batch(provider, SEC, as_of=date(2025, 5, 30),
                         rule=self.rule(days=1), now=NOW)
        self.assertEqual(len([c for c in provider.calls if c[0] == "type"]), 0)

    def test_as_of_must_be_before_today_in_new_york(self):
        with self.assertRaises(SelectionError) as raised:
            select_batch(FakeProvider({}), SEC, as_of=date(2025, 6, 2),
                         rule=self.rule(), now=NOW)
        self.assertEqual(str(raised.exception), "INPUT_INVALID")


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_batch_select`
Expected: `ModuleNotFoundError: No module named 'microcap_batch_select'`

- [ ] **Step 3: Implement the selection logic**

Create `microcap_batch_select.py`:

```python
"""Mechanical micro-cap runner-day selection for a coverage-only pilot; always NO_TRADE."""

from __future__ import annotations

import math
import re
import time
from collections import deque
from collections.abc import Callable, Mapping
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from microcap_batch_schema import (
    BIAS, MAX_CALENDAR_DAYS, SYMBOL_PATTERN, manifest_hash, session_window,
    validate_manifest, validate_rule,
)
from microcap_history import CoverageError


_NEW_YORK = ZoneInfo("America/New_York")
_TICKER = re.compile(SYMBOL_PATTERN + r"\Z")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")


class SelectionError(RuntimeError):
    """Fixed-code selection failure; message is a code, never provider text."""


class ProviderError(RuntimeError):
    """Fixed-code provider failure."""


class RateLimiter:
    def __init__(self, *, limit: int = 5, window: float = 60.0,
                 clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._limit = limit
        self._window = window
        self._clock = clock
        self._sleep = sleep
        self._starts: deque[float] = deque()

    def wait(self) -> None:
        now = self._clock()
        while self._starts and now - self._starts[0] >= self._window:
            self._starts.popleft()
        if len(self._starts) >= self._limit:
            self._sleep(self._starts[0] + self._window - now)
            now = self._clock()
            self._starts.popleft()
        self._starts.append(now)


def _finite(value: object) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def screen(rows: list, rule: Mapping) -> list[tuple[str, float]]:
    ranked = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        ticker, open_, high, volume = row.get("T"), row.get("o"), row.get("h"), row.get("v")
        if not (isinstance(ticker, str) and _TICKER.fullmatch(ticker)):
            continue
        if not (_finite(open_) and _finite(high) and _finite(volume)):
            continue
        if not rule["min_open"] <= open_ <= rule["max_open"] or open_ <= 0:
            continue
        if volume < rule["min_volume"]:
            continue
        move = high / open_ - 1
        if move < rule["min_move"]:
            continue
        ranked.append((ticker, round(move * 100, 1), move))
    ranked.sort(key=lambda item: (-item[2], item[0]))
    return [(ticker, move_pct) for ticker, move_pct, _ in ranked]


def parse_sec_tickers(payload: object) -> dict[str, tuple[str, str]]:
    if not isinstance(payload, Mapping):
        raise SelectionError("PROVIDER_ERROR")
    seen: dict[str, set[str]] = {}
    titles: dict[str, str] = {}
    for entry in payload.values():
        if not isinstance(entry, Mapping):
            continue
        cik, ticker, title = entry.get("cik_str"), entry.get("ticker"), entry.get("title")
        if type(cik) is not int or not 0 < cik < 10**10:
            continue
        if not (isinstance(ticker, str) and _TICKER.fullmatch(ticker)):
            continue
        if not (isinstance(title, str) and 0 < len(title) <= 120 and title.isprintable()):
            continue
        cik10 = f"{cik:010d}"
        seen.setdefault(ticker, set()).add(cik10)
        titles[ticker] = title
    return {ticker: (next(iter(ciks)), titles[ticker])
            for ticker, ciks in seen.items() if len(ciks) == 1}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def select_batch(provider, sec_map: Mapping[str, tuple[str, str]], *, as_of: date,
                 rule: Mapping, now: datetime) -> dict[str, object]:
    try:
        validate_rule(dict(rule))
    except CoverageError:
        raise SelectionError("INPUT_INVALID") from None
    if now.tzinfo is None or as_of >= now.astimezone(_NEW_YORK).date():
        raise SelectionError("INPUT_INVALID")
    trading_days, candidates, samples = [], [], []
    selected_ciks: set[str] = set()
    for offset in range(MAX_CALENDAR_DAYS):
        if len(trading_days) >= rule["days"]:
            break
        day = as_of - timedelta(days=offset)
        if day.weekday() >= 5:
            continue
        try:
            request_id, rows = provider.grouped(day)
        except ProviderError:
            raise SelectionError("PROVIDER_ERROR") from None
        if not rows:
            continue
        if not (isinstance(request_id, str) and _REQUEST_ID.fullmatch(request_id)):
            raise SelectionError("PROVIDER_ERROR")
        trading_days.append({"date": day.isoformat(), "request_id": request_id})
        accepted = 0
        for ticker, move_pct in screen(rows, rule)[: rule["max_candidates_per_day"]]:
            if accepted >= rule["per_day"]:
                break
            mapped = sec_map.get(ticker)
            if mapped is None:
                outcome = "NO_SEC_CIK"
            elif mapped[0] in selected_ciks:
                outcome = "DUPLICATE_ISSUER"
            else:
                try:
                    outcome = ("SELECTED" if provider.ticker_type(ticker, day) == "CS"
                               else "NOT_COMMON_STOCK")
                except ProviderError:
                    outcome = "PROVIDER_ERROR"
            candidates.append({"date": day.isoformat(), "ticker": ticker,
                               "move_pct": move_pct, "outcome": outcome})
            if outcome == "SELECTED":
                accepted += 1
                selected_ciks.add(mapped[0])
                start, end = session_window(day)
                samples.append({"issuer_id": mapped[0], "symbol": ticker,
                                "company": mapped[1], "date": day.isoformat(),
                                "move_pct": move_pct, "start_utc": start, "end_utc": end})
    if not samples:
        raise SelectionError("NO_SAMPLES_SELECTED")
    manifest = {
        "schema_version": 1, "kind": "microcap_batch_manifest",
        "created_at": _utc_text(now), "as_of": as_of.isoformat(), "rule": dict(rule),
        "trading_days": trading_days, "candidates": candidates, "samples": samples,
        "bias": list(BIAS),
    }
    manifest["sha256"] = manifest_hash(manifest)
    try:
        validate_manifest(manifest)
    except CoverageError:
        raise SelectionError("INPUT_INVALID") from None
    return manifest
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_batch_select test_microcap_batch_schema`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add microcap_batch_select.py test_microcap_batch_select.py
git commit -m "Add mechanical micro-cap runner-day selection logic

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: Selection HTTP clients and CLI

**Files:**
- Modify: `microcap_batch_select.py`
- Test: `test_microcap_batch_select.py`

- Redirects are refused. Reads are chunked and capped: 16 MiB for Massive grouped, 1 MiB for ticker details, 4 MiB for SEC tickers.
- The Massive key travels only in the query string of requests. It never appears in exceptions, output or files, because every failure maps to a fixed code.
- Massive grouped requires `status in ("OK", "DELAYED")`, an int `resultsCount >= 0`, and a list `results` (missing is allowed only when the count is 0). Ticker details requires a str `results.type`.
- SEC tickers requires `User-Agent: $SEC_USER_AGENT`.
- CLI:
  - Arguments: `--as-of` (default: New York today minus 1 day), `--days` (default 10, range 1..20), `--per-day` (default 2, range 1..5), `--output` (required, external).
  - Missing `MASSIVE_API_KEY` or `SEC_USER_AGENT` exits 2 with `{"error":"ENVIRONMENT_INCOMPLETE"}`.
  - Other exits: invalid input → 2 `{"error":"INPUT_INVALID"}`; provider error or no samples → 3 with the code.
  - Success → 0 and stdout `{"output":…,"samples":N,"sha256":…,"trading_days":K}`.

- [ ] **Step 1: Write the failing tests** (append to `test_microcap_batch_select.py`, before the `__main__` guard)

```python
import io
import json
import os
import tempfile
from pathlib import Path
from unittest import mock

import microcap_batch_select as selection


class FakeResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def routed(routes):
    def fake_open(request, timeout):
        url = request.full_url
        for fragment, body in routes.items():
            if fragment in url:
                if isinstance(body, Exception):
                    raise body
                return FakeResponse(json.dumps(body).encode())
        raise OSError("unrouted")
    return fake_open


GROUPED_OK = {"status": "OK", "resultsCount": 2, "request_id": "abc123",
              "results": [bar("AAA", 2.0, 4.0), bar("BBB", 2.0, 2.1)]}
GROUPED_EMPTY = {"status": "OK", "resultsCount": 0, "request_id": "def456"}
TICKERS = {"0": {"cik_str": 1, "ticker": "AAA", "title": "Alpha Inc"}}


class HttpClientTests(unittest.TestCase):
    def test_massive_grouped_and_type(self):
        limiter = RateLimiter(clock=lambda: 0.0, sleep=lambda s: None)
        client = selection.MassiveDaily("k-test", limiter)
        with mock.patch.object(selection, "_open", routed({
            "/grouped/locale/us/market/stocks/2025-05-30": GROUPED_OK,
            "/grouped/locale/us/market/stocks/2025-05-31": GROUPED_EMPTY,
            "/v3/reference/tickers/AAA": {"status": "OK", "results": {"type": "CS"}},
        })):
            self.assertEqual(client.grouped(date(2025, 5, 30))[0], "abc123")
            self.assertEqual(client.grouped(date(2025, 5, 31)), ("def456", []))
            self.assertEqual(client.ticker_type("AAA", date(2025, 5, 30)), "CS")

    def test_malformed_and_failing_responses_raise_fixed_code(self):
        client = selection.MassiveDaily("k-test", RateLimiter(clock=lambda: 0.0,
                                                               sleep=lambda s: None))
        for body in ({"status": "ERROR"}, {"status": "OK", "resultsCount": "2"},
                     {"status": "OK", "resultsCount": 2}, OSError("k-test leaked")):
            with self.subTest(body=body), mock.patch.object(
                    selection, "_open", routed({"/grouped/": body})):
                with self.assertRaises(ProviderError) as raised:
                    client.grouped(date(2025, 5, 30))
                self.assertEqual(str(raised.exception), "PROVIDER_ERROR")

    def test_oversized_body_is_rejected(self):
        def fake_open(request, timeout):
            return FakeResponse(b" " * (selection._TYPE_LIMIT + 1))
        client = selection.MassiveDaily("k", RateLimiter(clock=lambda: 0.0,
                                                          sleep=lambda s: None))
        with mock.patch.object(selection, "_open", fake_open):
            with self.assertRaises(ProviderError):
                client.ticker_type("AAA", date(2025, 5, 30))

    def test_redirects_are_refused(self):
        self.assertIsNone(selection._NoRedirect().redirect_request(
            None, None, 302, "Found", {}, "https://elsewhere.example/"))


class CliTests(unittest.TestCase):
    def run_cli(self, argv, env, routes):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(selection, "_open", routed(routes)), \
                mock.patch.object(selection, "_now",
                                  lambda: datetime(2025, 6, 2, 12, tzinfo=timezone.utc)), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = selection.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_successful_run_writes_valid_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp) / "batch-manifest.json"
            env = {"MASSIVE_API_KEY": "k-secret-value", "SEC_USER_AGENT": "Test test@example.com"}
            code, out, err = self.run_cli(
                ["--as-of", "2025-05-30", "--days", "1", "--output", str(output)], env,
                {"/grouped/": GROUPED_OK, "company_tickers.json": TICKERS,
                 "/v3/reference/tickers/AAA": {"status": "OK", "results": {"type": "CS"}}})
            self.assertEqual((code, err), (0, ""))
            manifest = json.loads(output.read_text())
            validate_manifest(manifest)
            self.assertEqual(json.loads(out)["samples"], 1)
            self.assertNotIn("k-secret-value", out + output.read_text())

    def test_missing_environment_and_provider_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp) / "m.json")
            code, _, err = self.run_cli(["--output", output], {}, {})
            self.assertEqual((code, json.loads(err)), (2, {"error": "ENVIRONMENT_INCOMPLETE"}))
            env = {"MASSIVE_API_KEY": "k-secret-value", "SEC_USER_AGENT": "Test test@example.com"}
            code, _, err = self.run_cli(["--as-of", "2025-05-30", "--output", output], env,
                                        {"company_tickers.json": TICKERS,
                                         "/grouped/": OSError("k-secret-value")})
            self.assertEqual((code, json.loads(err)), (3, {"error": "PROVIDER_ERROR"}))
            self.assertFalse(Path(output).exists())

    def test_relative_or_repository_output_is_invalid(self):
        env = {"MASSIVE_API_KEY": "k", "SEC_USER_AGENT": "T t@example.com"}
        code, _, err = self.run_cli(["--output", "relative.json"], env, {})
        self.assertEqual((code, json.loads(err)), (2, {"error": "INPUT_INVALID"}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_batch_select`
Expected: errors with `AttributeError: module 'microcap_batch_select' has no attribute 'MassiveDaily'` (and `_open`, `main`).

- [ ] **Step 3: Implement clients and CLI** (append to `microcap_batch_select.py`; add the new imports at the top)

New imports: `argparse`, `json`, `os`, `sys`, `urllib.error`, `urllib.parse`, `urllib.request`, `from pathlib import Path`, and `from microcap_batch_schema import DEFAULT_RULE`. Also add `from microcap_readiness import _external, _save, _unique_object, _reject_constant`.

```python
_MASSIVE = "https://api.massive.com"
_SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
_GROUPED_LIMIT = 16 * 1024 * 1024
_TYPE_LIMIT = 1024 * 1024
_SEC_LIMIT = 4 * 1024 * 1024
_TIMEOUT = 30


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _open(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _get_json(url: str, *, limit: int, headers: Mapping[str, str] | None = None) -> object:
    request = urllib.request.Request(url, headers={"Accept": "application/json",
                                                   **(headers or {})})
    try:
        with _open(request, _TIMEOUT) as response:
            if getattr(response, "status", 200) != 200:
                raise ProviderError("PROVIDER_ERROR")
            chunks, total = [], 0
            while chunk := response.read(65536):
                total += len(chunk)
                if total > limit:
                    raise ProviderError("PROVIDER_ERROR")
                chunks.append(chunk)
        return json.loads(b"".join(chunks), object_pairs_hook=_unique_object,
                          parse_constant=_reject_constant)
    except ProviderError:
        raise
    except Exception:
        raise ProviderError("PROVIDER_ERROR") from None


class MassiveDaily:
    def __init__(self, api_key: str, limiter: RateLimiter) -> None:
        self._key = api_key
        self._limiter = limiter

    def _url(self, path: str, params: Mapping[str, str]) -> str:
        query = urllib.parse.urlencode({**params, "apiKey": self._key})
        return f"{_MASSIVE}{path}?{query}"

    def grouped(self, day: date) -> tuple[str, list]:
        self._limiter.wait()
        body = _get_json(self._url(
            f"/v2/aggs/grouped/locale/us/market/stocks/{day.isoformat()}",
            {"adjusted": "false"}), limit=_GROUPED_LIMIT)
        if not isinstance(body, Mapping) or body.get("status") not in ("OK", "DELAYED"):
            raise ProviderError("PROVIDER_ERROR")
        count, results, request_id = (body.get("resultsCount"), body.get("results"),
                                      body.get("request_id"))
        if type(count) is not int or count < 0 or not isinstance(request_id, str):
            raise ProviderError("PROVIDER_ERROR")
        if results is None and count == 0:
            results = []
        if not isinstance(results, list):
            raise ProviderError("PROVIDER_ERROR")
        return request_id, results

    def ticker_type(self, ticker: str, day: date) -> str:
        if not _TICKER.fullmatch(ticker):
            raise ProviderError("PROVIDER_ERROR")
        self._limiter.wait()
        body = _get_json(self._url(f"/v3/reference/tickers/{ticker}",
                                   {"date": day.isoformat()}), limit=_TYPE_LIMIT)
        results = body.get("results") if isinstance(body, Mapping) else None
        kind = results.get("type") if isinstance(results, Mapping) else None
        if not isinstance(kind, str):
            raise ProviderError("PROVIDER_ERROR")
        return kind


def fetch_sec_tickers(user_agent: str) -> dict[str, tuple[str, str]]:
    try:
        payload = _get_json(_SEC_TICKERS, limit=_SEC_LIMIT,
                            headers={"User-Agent": user_agent})
    except ProviderError:
        raise SelectionError("PROVIDER_ERROR") from None
    return parse_sec_tickers(payload)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise SelectionError("INPUT_INVALID")


def _fail(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 2 if code in ("INPUT_INVALID", "ENVIRONMENT_INCOMPLETE") else 3


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Select micro-cap runner days for a coverage-only pilot; "
                                 "always NO_TRADE")
    parser.add_argument("--as-of", help="latest US trading date to consider (YYYY-MM-DD)")
    parser.add_argument("--days", type=int, default=DEFAULT_RULE["days"])
    parser.add_argument("--per-day", type=int, default=DEFAULT_RULE["per_day"])
    parser.add_argument("--output", required=True, help="absolute external manifest JSON")
    try:
        args = parser.parse_args(argv)
        api_key = os.environ.get("MASSIVE_API_KEY", "").strip()
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not api_key or not user_agent:
            return _fail("ENVIRONMENT_INCOMPLETE")
        now = _now()
        as_of = (date.fromisoformat(args.as_of) if args.as_of
                 else now.astimezone(_NEW_YORK).date() - timedelta(days=1))
        rule = {**DEFAULT_RULE, "days": args.days, "per_day": args.per_day}
        try:
            output = _external(Path(args.output), existing=False)
        except CoverageError:
            return _fail("INPUT_INVALID")
        sec_map = fetch_sec_tickers(user_agent)
        manifest = select_batch(MassiveDaily(api_key, RateLimiter()), sec_map,
                                as_of=as_of, rule=rule, now=now)
        _save(output, manifest)
    except SelectionError as error:
        return _fail(str(error))
    except (CoverageError, ValueError):
        return _fail("INPUT_INVALID")
    print(json.dumps({"output": str(output), "samples": len(manifest["samples"]),
                      "sha256": manifest["sha256"],
                      "trading_days": len(manifest["trading_days"])},
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_batch_select`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add microcap_batch_select.py test_microcap_batch_select.py
git commit -m "Add Massive/SEC clients and CLI for batch selection

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: Runner — per-sample stages and summary row

**Files:**
- Create: `microcap_batch_run.py`
- Test: `test_microcap_batch_run.py`

Design notes:
- A stage runner is `runner(script: str, args: list[str]) -> tuple[int, bytes]` (exit code, stdout). It raises `subprocess.TimeoutExpired` on timeout. The default `run_stage` uses `sys.executable`, `cwd` at the repository root, `stderr=DEVNULL`, and a 300 s timeout. The child inherits the environment.
- Per-sample directory `<out-dir>/<date>-<cik>/` (mode 700) contains:
  - `roster.json`: the pilot roster, one member, `valid_from` = date 00:00Z.
  - `ibkr-manifest.json`: predeclared, no symbol. The coverage probe input.
  - `source-manifest.json`: the same plus `symbol`. The source probe input.
  - `ibkr-report.json`: parsed IBKR stdout, re-serialized. Raw bytes are never stored.
  - `base-report.json`: the IBKR report with `sample_manifest.symbol` added.
  - `source-report.json`.
  - `sec-report.json`: written by the SEC CLI.
  - `sample-result.json`.
- Stage mapping:
  - IBKR: exit 0 → `OK`. Exit 3 with a JSON object on stdout → `PROVIDER_ERROR`, and the report is kept. Any other exit, or bad JSON → `PROVIDER_ERROR` with no report (bad JSON → `INVALID`). Timeout → `TIMEOUT`.
  - Source: runs only when the IBKR report exists, otherwise `SKIPPED`. Exit 0 with a JSON object → `OK`; exit 3 → `PROVIDER_ERROR`; other or bad JSON → `INVALID`.
  - SEC: always runs. Exit 0 and the output file exists → `OK`; exit 3 → `PROVIDER_ERROR`; otherwise `INVALID`.
- Summary (`summarize`):
  - With a source report, call `project_readiness(source, sec_or_None, now=…)`. If the SEC report fails validation, retry with `sec=None` and set the SEC stage to `INVALID`. If the projection fails, or its sample identity (issuer, symbol, date) or window differs from the manifest sample, set the source stage to `INVALID` and use the no-source path.
  - Without a valid source report, the row has `ibkr_minute`/`ibkr_quotes` false, `roster: "MISSING"`, `news_count: None`. SEC observations come from `_validate_sec(sec, issuer, start, end)` (`INVALID` → 0 and SEC stage `INVALID`). Blockers are the SEC blockers plus `SAMPLE_INCOMPLETE`, `MARKET_CAP_UNVERIFIED`, `ROSTER_COVERAGE_UNVERIFIED` and `RESEARCH_ONLY_NOT_CALIBRATED`.
  - `news_count` is the article count when the news status is `ARTICLES_OBSERVED` or `EMPTY`, otherwise `None`.
  - Any non-OK stage adds `SAMPLE_INCOMPLETE`.
- `sample-result.json`: `{"kind":"microcap_batch_sample_result","schema_version":1,"manifest_sha256",…,"stages":{"ibkr","source","sec"},"row":{…},"blockers":[…]}`. `row.stage_errors` is derived from `stages`.

- [ ] **Step 1: Write the failing tests**

Create `test_microcap_batch_run.py`:

```python
import json
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from microcap_batch_schema import DEFAULT_RULE, BIAS, manifest_hash, session_window
from microcap_batch_run import load_sample_result, run_sample
from microcap_coverage_probe import _roster_status, coverage_report
from microcap_source_probe import extend_coverage
from test_microcap_readiness import sec_report


NOW = datetime(2026, 10, 5, 12, 0, tzinfo=timezone.utc)
START, END = session_window(datetime(2025, 5, 28).date())


def sample():
    return {"issuer_id": "0000000001", "symbol": "SORA", "company": "Sora Example",
            "date": "2025-05-28", "move_pct": 52.3, "start_utc": START, "end_utc": END}


def ibkr_report():
    roster_status, _ = _roster_status({}, datetime(2025, 5, 28, tzinfo=timezone.utc),
                                      pilot=True)
    report = coverage_report(
        roster_status,
        [{"issuer_id": "0000000001", "date": "2025-05-28", "session": "regular",
          "provider": "ibkr",
          "bars": {"status": "observed",
                   "intervals": {"1 min": {"count": 3}, "1 hour": {"count": 2}}},
          "quotes": {"status": "observed"}, "news": {"status": "unavailable"}}],
        sample_manifest={"status": "predeclared", "issuer_ids": ["0000000001"],
                         "dates": ["2025-05-28"]},
    )
    report["request_window"] = {"start_utc": START, "end_utc": END}
    return report


def session_sec_report():
    value = sec_report()
    value["market_cap_gate"]["decision_at"] = START
    return value


def arg(args, name):
    return args[args.index(name) + 1]


class FakeRunner:
    def __init__(self, ibkr=(0, None), source=0, sec=0, timeout=()):
        self.ibkr, self.source, self.sec, self.timeout = ibkr, source, sec, set(timeout)
        self.calls = []

    def __call__(self, script, args):
        self.calls.append(script)
        if script in self.timeout:
            raise subprocess.TimeoutExpired(script, 300)
        if script == "microcap_coverage_probe.py":
            code, body = self.ibkr
            payload = body if body is not None else json.dumps(ibkr_report()).encode()
            return code, payload
        if script == "microcap_source_probe.py":
            base = json.loads(Path(arg(args, "--base-report")).read_text())
            report = extend_coverage(base, roster=None, alpaca_news=None, cap_evidence=None)
            return self.source, json.dumps(report).encode()
        if script == "microcap_sec_probe.py":
            if self.sec == 0:
                Path(arg(args, "--output")).write_text(json.dumps(session_sec_report()))
            return self.sec, b""
        raise AssertionError(script)


class RunSampleTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "2025-05-28-0000000001"

    def tearDown(self):
        self.tmp.cleanup()

    def run_with(self, runner):
        return run_sample(sample(), self.dir, runner=runner, manifest_sha256="a" * 64, now=NOW)

    def test_all_stages_ok(self):
        runner = FakeRunner()
        result = self.run_with(runner)
        self.assertEqual(runner.calls, ["microcap_coverage_probe.py",
                                        "microcap_source_probe.py", "microcap_sec_probe.py"])
        self.assertEqual(result["stages"], {"ibkr": "OK", "source": "OK", "sec": "OK"})
        row = result["row"]
        self.assertEqual((row["ibkr_minute"], row["ibkr_quotes"], row["sec_observations"],
                          row["stage_errors"]), (True, True, 1, []))
        self.assertEqual(row["roster"], "MISSING")
        self.assertIsNone(row["news_count"])
        self.assertEqual(load_sample_result(self.dir, sample(), "a" * 64), result)
        roster = json.loads((self.dir / "roster.json").read_text())
        self.assertEqual(roster["coverage_claim"], "pilot-unverified")
        self.assertEqual(roster["members"][0]["valid_from"], "2025-05-28T00:00:00Z")
        self.assertNotIn("symbol", json.loads((self.dir / "ibkr-manifest.json").read_text()))

    def test_ibkr_exit_3_keeps_report(self):
        result = self.run_with(FakeRunner(ibkr=(3, None)))
        self.assertEqual(result["stages"]["ibkr"], "PROVIDER_ERROR")
        self.assertEqual(result["stages"]["source"], "OK")
        self.assertTrue(result["row"]["ibkr_minute"])
        self.assertEqual(result["row"]["stage_errors"], ["ibkr:PROVIDER_ERROR"])
        self.assertIn("SAMPLE_INCOMPLETE", result["blockers"])

    def test_ibkr_failure_skips_source_but_sec_still_runs(self):
        for runner, status in ((FakeRunner(ibkr=(2, b"")), "PROVIDER_ERROR"),
                               (FakeRunner(ibkr=(0, b"not json")), "INVALID"),
                               (FakeRunner(timeout={"microcap_coverage_probe.py"}), "TIMEOUT")):
            with self.subTest(status=status):
                result = self.run_with(runner)
                self.assertNotIn("microcap_source_probe.py", runner.calls)
                self.assertEqual(result["stages"],
                                 {"ibkr": status, "source": "SKIPPED", "sec": "OK"})
                self.assertEqual(result["row"]["sec_observations"], 1)
                self.assertFalse(result["row"]["ibkr_minute"])

    def test_sec_failure_keeps_source_row(self):
        result = self.run_with(FakeRunner(sec=3))
        self.assertEqual(result["stages"]["sec"], "PROVIDER_ERROR")
        self.assertEqual(result["row"]["sec_observations"], 0)
        self.assertTrue(result["row"]["ibkr_quotes"])

    def test_identity_mismatch_marks_source_invalid(self):
        other = sample()
        other["issuer_id"] = "0000000002"
        result = run_sample(other, self.dir, runner=FakeRunner(),
                            manifest_sha256="a" * 64, now=NOW)
        self.assertEqual(result["stages"]["source"], "INVALID")
        self.assertFalse(result["row"]["ibkr_minute"])

    def test_tampered_or_foreign_result_is_not_resumed(self):
        self.run_with(FakeRunner())
        self.assertIsNone(load_sample_result(self.dir, sample(), "b" * 64))
        path = self.dir / "sample-result.json"
        value = json.loads(path.read_text())
        value["row"]["sec_observations"] = 9
        path.write_text(json.dumps(value))
        self.assertIsNone(load_sample_result(self.dir, sample(), "a" * 64))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_batch_run`
Expected: `ModuleNotFoundError: No module named 'microcap_batch_run'`

- [ ] **Step 3: Implement `microcap_batch_run.py` (sample level)**

```python
"""Run existing micro-cap research CLIs over a batch manifest; always NO_TRADE."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from datetime import datetime
from pathlib import Path

from microcap_batch_schema import (
    REQUIRED_BLOCKERS, STAGE_STATUSES, STAGES, validate_blockers, validate_row,
)
from microcap_history import CoverageError
from microcap_readiness import _load, _save, _validate_sec, project_readiness


_REPOSITORY = Path(__file__).resolve().parent
STAGE_TIMEOUT = 300
_MAX_STDOUT = 2 * 1024 * 1024
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_NEWS_OBSERVED = ("ARTICLES_OBSERVED", "EMPTY")

StageRunner = Callable[[str, list[str]], tuple[int, bytes]]


def run_stage(script: str, args: list[str]) -> tuple[int, bytes]:
    completed = subprocess.run(
        [sys.executable, str(_REPOSITORY / script), *args], cwd=_REPOSITORY,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=STAGE_TIMEOUT, check=False,
    )
    return completed.returncode, completed.stdout


def _parse_object(payload: bytes) -> dict | None:
    if len(payload) > _MAX_STDOUT:
        return None
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _write_inputs(sample: Mapping, directory: Path) -> tuple[Path, Path, Path]:
    roster = {
        "source_url": _SEC_TICKERS_URL, "retrieved_at": sample["start_utc"],
        "coverage_claim": "pilot-unverified",
        "members": [{
            "issuer_id": sample["issuer_id"], "symbol": sample["symbol"],
            "company": sample["company"], "valid_from": f"{sample['date']}T00:00:00Z",
            "valid_to": None, "listing_status": "listed",
            "source_record_id": f"sec-cik-{sample['issuer_id']}-{sample['symbol'].lower()}",
        }],
    }
    ibkr_manifest = {"status": "predeclared", "issuer_ids": [sample["issuer_id"]],
                     "dates": [sample["date"]]}
    paths = (directory / "roster.json", directory / "ibkr-manifest.json",
             directory / "source-manifest.json")
    _save(paths[0], roster)
    _save(paths[1], ibkr_manifest)
    _save(paths[2], {**ibkr_manifest, "symbol": sample["symbol"]})
    return paths


def _run_ibkr(runner: StageRunner, sample: Mapping, directory: Path,
              roster: Path, manifest: Path) -> tuple[str, dict | None]:
    try:
        code, stdout = runner("microcap_coverage_probe.py", [
            "--roster", str(roster), "--sample-manifest", str(manifest),
            "--start", sample["start_utc"], "--end", sample["end_utc"],
            "--pilot", "--fetch", "--max-symbols", "1", "--max-days", "1"])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code not in (0, 3):
        return "PROVIDER_ERROR", None
    report = _parse_object(stdout)
    if report is None:
        return "INVALID", None
    _save(directory / "ibkr-report.json", report)
    return ("OK" if code == 0 else "PROVIDER_ERROR"), report


def _run_source(runner: StageRunner, sample: Mapping, directory: Path,
                ibkr: Mapping, manifest: Path) -> tuple[str, dict | None]:
    base = json.loads(json.dumps(ibkr))
    if not isinstance(base.get("sample_manifest"), dict):
        return "INVALID", None
    base["sample_manifest"]["symbol"] = sample["symbol"]
    _save(directory / "base-report.json", base)
    try:
        code, stdout = runner("microcap_source_probe.py", [
            "--base-report", str(directory / "base-report.json"),
            "--sample-manifest", str(manifest),
            "--start", sample["start_utc"], "--end", sample["end_utc"],
            "--fetch", "--max-pages", "2"])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code == 3:
        return "PROVIDER_ERROR", None
    report = _parse_object(stdout) if code == 0 else None
    if report is None:
        return "INVALID", None
    _save(directory / "source-report.json", report)
    return "OK", report


def _run_sec(runner: StageRunner, sample: Mapping, directory: Path) -> tuple[str, dict | None]:
    output = directory / "sec-report.json"
    output.unlink(missing_ok=True)
    try:
        code, _ = runner("microcap_sec_probe.py", [
            "--cik", sample["issuer_id"], "--decision-at", sample["start_utc"],
            "--fetch", "--output", str(output)])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code == 3:
        return "PROVIDER_ERROR", None
    if code != 0 or not output.is_file():
        return "INVALID", None
    try:
        return "OK", _load(output)
    except CoverageError:
        return "INVALID", None


def summarize(sample: Mapping, stages: dict[str, str], source: Mapping | None,
              sec: Mapping | None, *, now: datetime) -> tuple[dict, list[str]]:
    projection = None
    if source is not None:
        for candidate in (sec, None) if sec is not None else (None,):
            try:
                projection = project_readiness(source, candidate, now=now)
            except CoverageError:
                continue
            if candidate is None and sec is not None:
                stages["sec"] = "INVALID"
            break
        expected = ({"issuer_id": sample["issuer_id"], "symbol": sample["symbol"],
                     "date": sample["date"]},
                    {"start_utc": sample["start_utc"], "end_utc": sample["end_utc"]})
        if projection is None or (projection["sample"], projection["sample_window"]) != expected:
            projection = None
            stages["source"] = "INVALID"
    if projection is not None:
        sources = projection["sources"]
        news = sources["news"]
        row_values = {
            "ibkr_minute": sources["ibkr"]["minute_cells"] > 0,
            "ibkr_quotes": sources["ibkr"]["quote_cells"] > 0,
            "roster": sources["roster"]["evidence_status"],
            "news_count": (news["article_count"]
                           if news["status"] in _NEWS_OBSERVED
                           and type(news["article_count"]) is int else None),
            "sec_observations": sources["sec"]["observation_count"],
        }
        blockers = set(projection["blockers"])
    else:
        sec_count, sec_blockers = 0, []
        if sec is not None:
            try:
                sec_count, sec_blockers = _validate_sec(
                    sec, issuer=sample["issuer_id"], start=sample["start_utc"],
                    end=sample["end_utc"])
            except CoverageError:
                stages["sec"] = "INVALID"
        row_values = {"ibkr_minute": False, "ibkr_quotes": False, "roster": "MISSING",
                      "news_count": None, "sec_observations": sec_count}
        blockers = set(sec_blockers) | {"SAMPLE_INCOMPLETE"}
    errors = sorted(f"{stage}:{stages[stage]}" for stage in STAGES if stages[stage] != "OK")
    if errors:
        blockers.add("SAMPLE_INCOMPLETE")
    row = {"date": sample["date"], "symbol": sample["symbol"],
           "issuer_id": sample["issuer_id"], "move_pct": sample["move_pct"],
           **row_values, "stage_errors": errors}
    validate_row(row)
    return row, sorted(blockers | REQUIRED_BLOCKERS)


def _result(sample, stages, row, blockers, manifest_sha256) -> dict:
    return {"kind": "microcap_batch_sample_result", "schema_version": 1,
            "manifest_sha256": manifest_sha256, "stages": stages, "row": row,
            "blockers": blockers}


def run_sample(sample: Mapping, directory: Path, *, runner: StageRunner,
               manifest_sha256: str, now: datetime) -> dict:
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / "sample-result.json").unlink(missing_ok=True)
    roster, ibkr_manifest, source_manifest = _write_inputs(sample, directory)
    stages = {"ibkr": "SKIPPED", "source": "SKIPPED", "sec": "SKIPPED"}
    stages["ibkr"], ibkr = _run_ibkr(runner, sample, directory, roster, ibkr_manifest)
    source = None
    if ibkr is not None:
        stages["source"], source = _run_source(runner, sample, directory, ibkr,
                                               source_manifest)
    stages["sec"], sec = _run_sec(runner, sample, directory)
    row, blockers = summarize(sample, stages, source, sec, now=now)
    result = _result(sample, stages, row, blockers, manifest_sha256)
    _save(directory / "sample-result.json", result)
    return result


def load_sample_result(directory: Path, sample: Mapping, manifest_sha256: str) -> dict | None:
    path = directory / "sample-result.json"
    if not path.is_file():
        return None
    try:
        value = _load(path)
        stages, row = value.get("stages"), value.get("row")
        if (set(value) != {"kind", "schema_version", "manifest_sha256", "stages", "row",
                           "blockers"}
                or value["kind"] != "microcap_batch_sample_result"
                or value["schema_version"] != 1
                or value["manifest_sha256"] != manifest_sha256
                or not isinstance(stages, dict) or set(stages) != set(STAGES)
                or any(status not in STAGE_STATUSES for status in stages.values())):
            return None
        validate_row(row)
        validate_blockers(value["blockers"])
        expected_errors = sorted(f"{s}:{stages[s]}" for s in STAGES if stages[s] != "OK")
        if (row["stage_errors"] != expected_errors
                or (row["date"], row["symbol"], row["issuer_id"], row["move_pct"])
                != (sample["date"], sample["symbol"], sample["issuer_id"],
                    sample["move_pct"])):
            return None
        if row["sec_observations"] and stages["sec"] != "OK":
            return None
    except CoverageError:
        return None
    return value
```

Note: `_save` refuses nothing about location by itself. `run_sample` is only called with directories that `run_batch` (Task 5) has already validated as external.

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_batch_run`
Expected: `OK` (6 tests). If `test_tampered_or_foreign_result_is_not_resumed` fails, check the `sec_observations` value 9: `validate_row` must reject it, because it is greater than 2.

- [ ] **Step 5: Commit**

```bash
git add microcap_batch_run.py test_microcap_batch_run.py
git commit -m "Add per-sample batch runner over existing research CLIs

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Runner — batch loop, resume, pacing, CLI

**Files:**
- Modify: `microcap_batch_run.py`
- Test: `test_microcap_batch_run.py`

- `run_batch(manifest, out_dir, *, runner, resume, now_fn, clock, sleep, spacing=25.0)`:
  - Validates the manifest, then processes samples in order.
  - Consecutive *executed* samples start at least `spacing` seconds apart. Resumed samples don't count.
  - Builds and validates a `microcap_batch_report`, then writes `<out-dir>/batch-report.json` atomically.
  - Batch blockers are the union of the sample blockers.
- CLI:
  - Arguments: `--manifest /abs/batch-manifest.json --out-dir /abs/dir [--resume]`.
  - The env pre-check requires `IB_PROBE_CLIENT_ID`, `MASSIVE_API_KEY`, `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY` and `SEC_USER_AGENT` to be non-empty; otherwise exit 2 `{"error":"ENVIRONMENT_INCOMPLETE"}`.
  - `--out-dir` must be an existing absolute directory outside the repository roots.
  - Progress goes to stderr, one compact JSON line per sample: `{"sample":i,"of":n,"symbol":…,"date":…,"stage_errors":[…],"resumed":bool}`.
  - Success → exit 0, with stdout `{"batch_report":…,"coverage":…,"samples":n}`. Invalid input → 2 `{"error":"INPUT_INVALID"}`.

- [ ] **Step 1: Write the failing tests** (append to `test_microcap_batch_run.py` before the `__main__` guard)

```python
import io
import os
from unittest import mock

import microcap_batch_run as batch_run
from microcap_batch_schema import validate_batch_report


def batch_manifest():
    second = dict(sample(), issuer_id="0000000002", symbol="ABCD", move_pct=40.0)
    body = {"schema_version": 1, "kind": "microcap_batch_manifest",
            "created_at": "2026-10-05T10:00:00Z", "as_of": "2025-05-28",
            "rule": dict(DEFAULT_RULE),
            "trading_days": [{"date": "2025-05-28", "request_id": "req1"}],
            "candidates": [], "samples": [sample(), second], "bias": list(BIAS)}
    body["sha256"] = manifest_hash(body)
    return body


class Clock:
    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(round(seconds, 3))
        self.now += seconds


class RunBatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_batch(self, runner, clock, resume=False, manifest=None):
        return batch_run.run_batch(manifest or batch_manifest(), self.out, runner=runner,
                                   resume=resume, now_fn=lambda: NOW, clock=clock,
                                   sleep=clock.sleep)

    def test_batch_report_aggregates_and_paces(self):
        clock = Clock()
        report = self.run_batch(FakeRunner(), clock)
        validate_batch_report(report)
        self.assertEqual(clock.sleeps, [25.0])
        self.assertEqual(report["batch"]["sample_count"], 2)
        self.assertEqual(report["coverage"]["ibkr_minute"], {"observed": 1, "total": 2})
        self.assertEqual(report["samples"][1]["stage_errors"],
                         ["sec:INVALID", "source:INVALID"])
        self.assertIn("SAMPLE_INCOMPLETE", report["blockers"])
        saved = json.loads((self.out / "batch-report.json").read_text())
        self.assertEqual(saved, report)

    def test_resume_skips_completed_samples_without_pacing(self):
        self.run_batch(FakeRunner(), Clock())
        runner, clock = FakeRunner(), Clock()
        report = self.run_batch(runner, clock, resume=True)
        self.assertEqual(runner.calls, [])
        self.assertEqual(clock.sleeps, [])
        validate_batch_report(report)

    def test_tampered_manifest_is_refused(self):
        manifest = batch_manifest()
        manifest["samples"][0]["move_pct"] = 99.0
        with self.assertRaises(batch_run.CoverageError):
            self.run_batch(FakeRunner(), Clock(), manifest=manifest)
        self.assertFalse((self.out / "batch-report.json").exists())


# Distinct fake values: the leak check scans reports for every configured credential.
ENV = {"IB_PROBE_CLIENT_ID": "177", "MASSIVE_API_KEY": "k-secret",
       "APCA_API_KEY_ID": "fake-apca-id", "APCA_API_SECRET_KEY": "fake-apca-secret",
       "SEC_USER_AGENT": "Test Agent test@example.com"}


class RunCliTests(unittest.TestCase):
    def main(self, argv, env):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(batch_run, "run_stage", FakeRunner()), \
                mock.patch.object(batch_run, "_sleep", lambda s: None), \
                mock.patch("sys.stdout", out), mock.patch("sys.stderr", err):
            code = batch_run.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_cli_runs_and_reports_progress(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "batch-manifest.json"
            manifest.write_text(json.dumps(batch_manifest()))
            out_dir = Path(tmp) / "runs"
            out_dir.mkdir()
            code, out, err = self.main(["--manifest", str(manifest), "--out-dir",
                                        str(out_dir)], ENV)
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(out)["samples"], 2)
            self.assertEqual(len(err.strip().splitlines()), 2)
            self.assertNotIn("k-secret", out + err)

    def test_cli_environment_and_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "m.json"
            manifest.write_text(json.dumps(batch_manifest()))
            args = ["--manifest", str(manifest), "--out-dir", tmp]
            missing = dict(ENV, APCA_API_SECRET_KEY="")
            self.assertEqual(self.main(args, missing)[0], 2)
            code, _, err = self.main(["--manifest", str(manifest), "--out-dir",
                                      str(Path(__file__).resolve().parent)], ENV)
            self.assertEqual((code, json.loads(err)), (2, {"error": "INPUT_INVALID"}))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_batch_run`
Expected: `AttributeError: module 'microcap_batch_run' has no attribute 'run_batch'`

- [ ] **Step 3: Implement** (append to `microcap_batch_run.py`; add imports `argparse`, `time`, `from datetime import timezone`, `from microcap_batch_schema import BIAS, coverage_from_rows, validate_batch_report, validate_manifest`, and `from microcap_readiness import _external, _repository_roots`)

```python
_REQUIRED_ENV = ("IB_PROBE_CLIENT_ID", "MASSIVE_API_KEY", "APCA_API_KEY_ID",
                 "APCA_API_SECRET_KEY", "SEC_USER_AGENT")
SAMPLE_SPACING = 25.0
_sleep = time.sleep


def _progress(index: int, total: int, row: Mapping, resumed: bool) -> None:
    print(json.dumps({"sample": index, "of": total, "symbol": row["symbol"],
                      "date": row["date"], "stage_errors": row["stage_errors"],
                      "resumed": resumed}, separators=(",", ":")),
          file=sys.stderr, flush=True)


def run_batch(manifest: Mapping, out_dir: Path, *, runner: StageRunner, resume: bool,
              now_fn: Callable[[], datetime], clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              spacing: float = SAMPLE_SPACING, progress: bool = False) -> dict:
    validate_manifest(manifest)
    samples = manifest["samples"]
    rows, blockers, last_start = [], set(), None
    for index, sample in enumerate(samples, start=1):
        directory = out_dir / f"{sample['date']}-{sample['issuer_id']}"
        result = (load_sample_result(directory, sample, manifest["sha256"])
                  if resume else None)
        resumed = result is not None
        if result is None:
            if last_start is not None and clock() - last_start < spacing:
                sleep(spacing - (clock() - last_start))
            last_start = clock()
            result = run_sample(sample, directory, runner=runner,
                                manifest_sha256=manifest["sha256"], now=now_fn())
        rows.append(result["row"])
        blockers |= set(result["blockers"])
        if progress:
            _progress(index, len(samples), result["row"], resumed)
    report = {
        "kind": "microcap_batch_report", "schema_version": 1,
        "generated_at": now_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decision": "NO_TRADE", "order_approval": False, "model_calibrated": False,
        "target_probabilities": "unavailable",
        "batch": {"as_of": manifest["as_of"], "manifest_sha256": manifest["sha256"],
                  "sample_count": len(rows), "rule": manifest["rule"], "bias": list(BIAS)},
        "coverage": coverage_from_rows(rows), "samples": rows,
        "blockers": sorted(blockers),
    }
    validate_batch_report(report)
    _save(out_dir / "batch-report.json", report)
    return report


def _external_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CoverageError("INPUT_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CoverageError("INPUT_INVALID") from None
    if (not resolved.is_dir()
            or any(resolved.is_relative_to(root) for root in _repository_roots())):
        raise CoverageError("INPUT_INVALID")
    return resolved


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageError("INPUT_INVALID")


def _fail(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Run the micro-cap coverage pilot over a batch manifest; "
                                 "always NO_TRADE")
    parser.add_argument("--manifest", required=True, help="absolute external batch manifest")
    parser.add_argument("--out-dir", required=True, help="absolute external output directory")
    parser.add_argument("--resume", action="store_true",
                        help="reuse valid sample-result.json files")
    try:
        args = parser.parse_args(argv)
        if any(not os.environ.get(name, "").strip() for name in _REQUIRED_ENV):
            return _fail("ENVIRONMENT_INCOMPLETE")
        manifest = _load(_external(Path(args.manifest), existing=True))
        out_dir = _external_dir(args.out_dir)
        report = run_batch(manifest, out_dir, runner=run_stage, resume=args.resume,
                           now_fn=lambda: datetime.now(timezone.utc), sleep=_sleep,
                           progress=True)
    except CoverageError:
        return _fail("INPUT_INVALID")
    print(json.dumps({"batch_report": str(out_dir / "batch-report.json"),
                      "coverage": report["coverage"], "samples": len(report["samples"])},
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

Note: tests patch the module attributes `run_stage` and `_sleep`. `main` reads both at call time, so the patches take effect.

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_batch_run test_microcap_batch_select test_microcap_batch_schema`
Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add microcap_batch_run.py test_microcap_batch_run.py
git commit -m "Add batch loop, resume, pacing and CLI for micro-cap pilot runner

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Publish schema 2 (`--batch-report`) and accept it in the store

**Files:**
- Modify: `microcap_readiness.py` (add `publish_batch_readiness`, update `main`)
- Modify: `microcap_readiness_store.py` (dispatch on schema version)
- Test: `test_microcap_readiness.py`, `test_microcap_readiness_store.py`

- [ ] **Step 1: Write the failing tests**

Append a new class to `test_microcap_readiness.py` (before the `__main__` guard):

```python
from test_microcap_batch_schema import batch_report as batch_report_fixture


class PublishBatchReadinessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.directory = Path(self.temp.name)
        self.batch_path = self.directory / "batch-report.json"
        self.status_path = self.directory / "status.json"
        self.batch_path.write_text(json.dumps(batch_report_fixture()), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def cli(self, argv):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            code = main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_publishes_schema_2_snapshot(self):
        code, out, err = self.cli(["--batch-report", str(self.batch_path),
                                   "--output", str(self.status_path)])
        self.assertEqual((code, err), (0, ""))
        saved = json.loads(self.status_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, json.loads(out))
        self.assertEqual(saved["schema_version"], 2)
        self.assertEqual(saved["coverage"]["ibkr_minute"], {"observed": 1, "total": 1})

    def test_source_and_batch_are_mutually_exclusive_and_sec_needs_source(self):
        for argv in (
            ["--batch-report", str(self.batch_path), "--source-report", str(self.batch_path),
             "--output", str(self.status_path)],
            ["--batch-report", str(self.batch_path), "--sec-report", str(self.batch_path),
             "--output", str(self.status_path)],
            ["--output", str(self.status_path)],
        ):
            with self.subTest(argv=argv):
                code, out, err = self.cli(argv)
                self.assertEqual((code, out), (2, ""))
                self.assertEqual(json.loads(err), {"error": "INPUT_INVALID"})
                self.assertFalse(self.status_path.exists())

    def test_inconsistent_batch_report_is_refused(self):
        value = batch_report_fixture()
        value["coverage"]["sec_shares"]["observed"] = 0
        self.batch_path.write_text(json.dumps(value), encoding="utf-8")
        code, _, _ = self.cli(["--batch-report", str(self.batch_path),
                               "--output", str(self.status_path)])
        self.assertEqual(code, 2)
        self.assertFalse(self.status_path.exists())
```

Append to `ReadReadinessTests` in `test_microcap_readiness_store.py`:

```python
    def test_schema_2_snapshot_is_current_and_forged_one_is_unavailable(self):
        from microcap_batch_schema import project_batch_snapshot
        from test_microcap_batch_schema import batch_report
        value = project_batch_snapshot(batch_report(), now=NOW - timedelta(seconds=30))
        result = configured_read(value)
        self.assertEqual(result["status"], "CURRENT")
        self.assertEqual(result["schema_version"], 2)
        self.assertEqual(result["samples"], value["samples"])
        forged = copy.deepcopy(value)
        forged["order_approval"] = True
        self.assertEqual(configured_read(forged)["status"], "UNAVAILABLE")
        forged = copy.deepcopy(value)
        forged["schema_version"] = 3
        self.assertEqual(configured_read(forged)["status"], "UNAVAILABLE")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `$PY -m unittest test_microcap_readiness test_microcap_readiness_store`
Expected: the new CLI test fails (exit 2 because `--batch-report` is unknown), and the store test fails (`UNAVAILABLE` != `CURRENT`).

- [ ] **Step 3: Implement**

In `microcap_readiness.py`, add after `publish_readiness`:

```python
def publish_batch_readiness(batch_path: Path, status_path: Path) -> dict[str, object]:
    """Validate an external batch report and atomically publish a schema 2 snapshot."""
    from microcap_batch_schema import project_batch_snapshot

    source = _external(batch_path, existing=True)
    destination = _external(status_path, existing=False)
    if destination == source:
        raise _invalid()
    result = project_batch_snapshot(_load(source), now=datetime.now(timezone.utc))
    _save(destination, result)
    return result
```

Replace the body of `main` with:

```python
def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Publish a sanitized, always-NO_TRADE micro-cap readiness snapshot")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--source-report", help="absolute external source report JSON (schema 1)")
    group.add_argument("--batch-report", help="absolute external batch report JSON (schema 2)")
    parser.add_argument("--output", required=True, help="absolute external readiness snapshot JSON")
    parser.add_argument("--sec-report", help="optional absolute external SEC pilot JSON "
                                             "(only with --source-report)")
    try:
        args = parser.parse_args(argv)
        if args.batch_report:
            if args.sec_report:
                raise _invalid()
            result = publish_batch_readiness(Path(args.batch_report), Path(args.output))
        else:
            result = publish_readiness(Path(args.source_report), Path(args.output),
                                       Path(args.sec_report) if args.sec_report else None)
    except CoverageError:
        diagnostic = _error_output()
        if diagnostic is not None:
            print(diagnostic, file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0
```

In `microcap_readiness_store.py`, add the import `from microcap_batch_schema import validate_snapshot_v2`. Then add a dispatcher and use it in `read_readiness`:

```python
def _validate_any(value: object) -> datetime:
    if type(value) is dict and type(value.get("schema_version")) is int \
            and value["schema_version"] == 2:
        return validate_snapshot_v2(value)
    return _validate(value)
```

In `read_readiness`, replace `generated_at = _validate(generated_file)` with `generated_at = _validate_any(generated_file)`. `CoverageError` is already caught there.

- [ ] **Step 4: Run tests to verify they pass**

Run: `$PY -m unittest test_microcap_readiness test_microcap_readiness_store test_microcap_batch_schema`
Expected: `OK`. The existing test that sets `schema_version=2` on a schema 1 body still expects `UNAVAILABLE`. It passes because `validate_snapshot_v2` rejects the schema 1 keys.

- [ ] **Step 5: Commit**

```bash
git add microcap_readiness.py microcap_readiness_store.py test_microcap_readiness.py test_microcap_readiness_store.py
git commit -m "Publish and read schema 2 micro-cap batch readiness snapshots

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 7: Operations docs and full verification

**Files:**
- Modify: `OPERATIONS.md` (new section after "Micro-cap Research Readiness Panel")
- Modify: `docs/superpowers/specs/2026-10-05-microcap-multi-sample-pilot-design.md` (symbol pattern)

- [ ] **Step 1: Add the docs section**

Append this section to `OPERATIONS.md`, directly after the readiness panel section:

````markdown
## Micro-cap Batch Coverage Pilot

Research only. Measures how often IBKR, Massive, Alpaca and SEC cover a mechanically selected batch of recent runner days. Every output is `NO_TRADE`. The selection uses the day's high (`SELECTION_USES_SAME_DAY_OUTCOME`), so these samples must never be used to calibrate or evaluate entry rules.

All paths are absolute and outside the repository. Load the private environment without printing it (`set -a; . ~/.config/alpaca/microcap.env; . ~/.config/tradingmax/sec.env; set +a`, plus `MASSIVE_API_KEY` and `IB_PROBE_CLIENT_ID=177`).

1. Select (Massive grouped daily and ticker type, SEC tickers; at most 5 Massive calls per minute):

   ```bash
   ./venv/bin/python microcap_batch_select.py --days 10 --per-day 2 --output /external/batch/batch-manifest.json
   ```

   Exit codes: 0 = ok; 2 = invalid input or `ENVIRONMENT_INCOMPLETE`; 3 = `PROVIDER_ERROR` or `NO_SAMPLES_SELECTED`.

2. Run every sample through the existing IBKR, source and SEC CLIs. This takes about 25 s or more per sample. TWS paper on port 7497 must be up.

   ```bash
   ./venv/bin/python microcap_batch_run.py --manifest /external/batch/batch-manifest.json --out-dir /external/batch/runs [--resume]
   ```

   Each sample gets `<out-dir>/<date>-<cik>/` holding the stage inputs and outputs and `sample-result.json`. `--resume` reuses valid results. A failing stage is recorded as `<stage>:<PROVIDER_ERROR|INVALID|TIMEOUT|SKIPPED>` and never aborts the batch.

3. Publish a schema 2 snapshot for the dashboard:

   ```bash
   ./venv/bin/python microcap_readiness.py --batch-report /external/batch/runs/batch-report.json --output "$MICROCAP_READINESS_PATH"
   ```

   `--batch-report` and `--source-report` are mutually exclusive. `--sec-report` applies only to `--source-report`.
````

- [ ] **Step 2: (Done during planning) The spec already uses `[A-Z]{1,6}` and lists `symbol_pattern` in `rule`.**

- [ ] **Step 3: Full verification**

Run: `$PY -m unittest test_microcap_batch_schema test_microcap_batch_select test_microcap_batch_run test_microcap_readiness test_microcap_readiness_store test_microcap_sec_evidence test_microcap_source_probe test_microcap_coverage_probe`
Expected: `OK`, with no failures or errors.

Run: `node test_microcap_research_panel.js`
Expected: passes (unchanged by Plan A).

- [ ] **Step 4: Commit**

```bash
git add OPERATIONS.md
git commit -m "Document micro-cap batch coverage pilot

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 8: Real batch run (controller only, after merge and the Plan B rollout restart)

Credentials stay in the environment of a single shell. Never print or `cat` the env files.

- [ ] **Step 1:** Create `/home/oferke/.local/share/tradingmax/research/batch-<as_of>/` (mode 700).
- [ ] **Step 2:** In one shell:
  - Run `set -a; . ~/.config/alpaca/microcap.env; . ~/.config/tradingmax/sec.env; set +a`.
  - Run `export MASSIVE_API_KEY="$(sed -n 's/^massive api key:[[:space:]]*//Ip' ~/.config/tradingmax/research-providers.env | head -1)"` and `export IB_PROBE_CLIENT_ID=177`.
  - Run selection, then the batch (`--resume` on rerun). Show the user only the stdout summaries and per-sample progress lines.
- [ ] **Step 3:** Publish with `microcap_readiness.py --batch-report …/runs/batch-report.json --output /home/oferke/.local/share/tradingmax/research/readiness.json`. The authenticated `/microcap-research-status` must return `schema_version: 2` and `status: CURRENT`.
- [ ] **Step 4:** Report the coverage numbers to the user and note the blockers that remain: roster completeness, market cap and calibration. No trading claims.
