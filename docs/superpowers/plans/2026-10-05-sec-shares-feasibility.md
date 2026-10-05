# SEC Shares Feasibility Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Determine whether bounded SEC public filings yield point-in-time outstanding-share evidence for a predeclared issuer/date, without clearing the existing market-cap gate.

**Architecture:** A bounded SEC reader collects submissions and companyfacts independently of broker code; a pure matcher records accession/acceptance/share facts and explicit gaps. An offline-first CLI writes a research-only evidence report outside git, never sets the `classify_microcap` audited-source witness and never places orders.

**Tech Stack:** Python 3.10, `urllib.request`, `datetime`, `unittest`, existing `microcap_history.CoverageError`/`parse_utc`; no new dependencies.

---

## Isolation and contract

Work from an isolated `.worktrees/` branch based on current `master`; the main checkout has unrelated dirty live-bot files. Never add keys, licensed data, provider response bodies or raw filings to git. Do not push/merge without a later explicit decision. The SEC pilot is about **feasibility**, not historical coverage or a qualifying verified cap source. Bounded public requests require `SEC_USER_AGENT` in the environment, e.g. a privately configured identifying application/contact; do not echo it into reports. One CIK, one decision instant, at most two HTTP GETs and at most two matching accessions per pilot. SEC `submissions/CIK##########.json` and `api/xbrl/companyfacts/CIK##########.json` are the only endpoints. Archived submission-index pages, raw filing text and prices are out of scope: absent matches are `COVERAGE_UNVERIFIED`, never proof of no shares.

## Task 1: Read-only SEC transport

**Files:** Create `microcap_sec_reader.py`, `test_microcap_sec_reader.py`.

- [ ] **Step 1: Add offline tests** for `SecReader.from_environment()` requiring nonempty `SEC_USER_AGENT`, `fetch(cik: str) -> tuple[dict,dict]` using fake `urlopen`, CIK exactly 1–10 ASCII digits normalized to 10 digits, two HTTPS host-pinned paths, no redirects to other hosts, timeout and per-response byte cap, HTTP 403/429/5xx and malformed JSON explicit `SEC_ACCESS_UNAVAILABLE`/`SEC_RATE_LIMITED`/`SEC_RESPONSE_INVALID`; no partial pair or token/body in errors. Sample:

  ```python
  with patch.dict(os.environ, {"SEC_USER_AGENT": ""}), patch("microcap_sec_reader.urlopen") as open_url:
      with self.assertRaisesRegex(CoverageError, "SEC_USER_AGENT_MISSING"):
          SecReader.from_environment()
      open_url.assert_not_called()
  ```

- [ ] **Step 2: Run** `./venv/bin/python -m unittest test_microcap_sec_reader -q`; expect missing-module failure.
- [ ] **Step 3: Implement** a focused `SecReader(user_agent: str, opener=urlopen)` whose `fetch` requests only those fixed URLs, uses `Request(..., headers={"User-Agent": self.user_agent, "Accept-Encoding": "identity"})`, checks final `response.geturl()` is the originally requested HTTPS URL, reads at most a fixed size plus one byte and parses JSON objects. Validate CIK before HTTP; return both raw objects only to the caller in memory. Convert only expected HTTP/URL/socket failures to fixed error codes; do not expose exception strings. Cover both status codes and `Retry-After` presence without automatic unbounded retry.
- [ ] **Step 4: Run** the targeted suite; expect PASS. Commit only these two files with `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>`.

## Task 2: As-of filing/share observation

**Files:** Create `microcap_sec_evidence.py`, `test_microcap_sec_evidence.py`.

- [ ] **Step 1: Add failing pure tests** for `observe_shares(submissions: Mapping, companyfacts: Mapping, *, cik: str, decision_at: str, fetched_at: str, max_accessions: int = 2) -> dict[str,object]`. Fake submissions `filings.recent` arrays include `accessionNumber`, `acceptanceDateTime`, `form`, `reportDate`; fake companyfacts include `cik` and `facts.dei.EntityCommonStockSharesOutstanding.units.shares` entries with `accn`, `end`, `val`, `filed`. Assert records join by accession, are tagged with UTC acceptance/report/fetch, and count is positive integer (not authorized or weighted shares). Example:

  ```python
  result = observe_shares(submissions, companyfacts, cik="0000123456",
                          decision_at="2025-05-28T15:00:00Z",
                          fetched_at="2026-10-05T11:00:00Z")
  self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
  self.assertFalse(result["source_verified"])
  ```

- [ ] **Step 2: Run** `./venv/bin/python -m unittest test_microcap_sec_evidence -q`; expect missing-module failure.
- [ ] **Step 3: Implement** strict CIK match, aligned submissions arrays, ISO acceptance timestamp with explicit timezone (SEC timestamp strings without offset require documented SEC timezone conversion, including DST; never assume UTC), `accepted_at <= decision_at`, accession match, supported form (`10-K`, `10-Q`, `8-K` and amended equivalents separately flagged), `filed` as a date-only cross-check not a publication time, and no more than two candidate accessions. Record observed share fact and accession only if all fields validate; missing/ambiguous timestamps, class coverage, amendments, stale archive, contradictory values or absent facts add precise blockers. Never set `source_verified=True`, `classes_complete=True`, or a market-cap classification in this pilot. Result contains fixed `status="MARKET_CAP_UNVERIFIED"`, `source_verified=False`, decision/CIK, bounded observations, sorted blockers and `coverage="UNVERIFIED"`. Reject malformed issuer, values, timezone and arrays instead of guessing.
- [ ] **Step 4: Test** exact acceptance boundary, later accepted filing with earlier `end`, DST transition, amended filing, multiple classes, reused/mismatched CIK, no matching filing and nonpositive/malformed shares. Run targeted suite and commit the two files with trailer.

## Task 3: Offline-first pilot and handoff

**Files:** Create `microcap_sec_probe.py`, `test_microcap_sec_probe.py`; modify `OPERATIONS.md`.

- [ ] **Step 1: Add failing CLI tests** for `--cik 0000123456 --decision-at 2025-05-28T15:00:00Z --submissions /external/submissions.json --companyfacts /external/companyfacts.json`; fake saved JSON, assert no `SecReader` construction and JSON `NO_TRADE`/false approvals/unavailable probabilities/unverified cap. `--fetch` must reject saved-input flags, require explicit `--output` absolute external file and `SEC_USER_AGENT`, one issuer/instant only. Invalid input exits 2; denied/rate-limited/timeout exits 3 with sanitized stderr and no success JSON. No raw body or user agent in output.
- [ ] **Step 2: Run** `./venv/bin/python -m unittest test_microcap_sec_probe -q`; expect missing-module failure.
- [ ] **Step 3: Implement** an argparse `main(argv=None) -> int`, validate input/paths before network, offline saved JSON by default; explicit `--fetch` calls the two fixed SEC endpoints once each via `SecReader`. Call `observe_shares`, emit JSON only with the fixed `NO_TRADE` fields. Save output atomically with `os.replace` to the specified external location (not in repo); do not serialize raw responses or auth headers. Fail closed for missing/stale evidence. Show exact CLI usage and all remaining blockers in `OPERATIONS.md`; document SEC access failure if a bounded real pilot cannot run.
- [ ] **Step 4: Run** `./venv/bin/python -m unittest test_microcap_sec_reader test_microcap_sec_evidence test_microcap_sec_probe test_microcap_cap_evidence -q`, `./venv/bin/python microcap_sec_probe.py --help`, and `git diff --check`. Commit the CLI/tests/docs with trailer.

## Gate

Review each task for spec compliance then code quality. Run the combined existing eleven-module micro-cap suite plus these three new modules. A single SEC sample cannot establish complete historical shares; if SEC access or class/filing reconciliation fails, keep `MARKET_CAP_UNVERIFIED` and stop. Only a separately reviewed source audit may change the audited-source witness. Do not begin Phase 2 live evaluation or Phase 3 paper orders.
