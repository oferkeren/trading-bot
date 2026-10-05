# Micro-cap Research Readiness Dashboard Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show sanitized, saved micro-cap research readiness and blockers in a dedicated dashboard panel without changing the live strategy or triggering provider/broker requests.

**Architecture:** An offline publisher reduces a validated `microcap_source_probe` report (and optional SEC feasibility output) to one atomic, allowlisted JSON snapshot. An authenticated read-only server endpoint loads that configured file with size/version/age validation; a standalone browser renderer displays it above the existing scanner and fails visibly closed when unavailable.

**Tech Stack:** Python 3.10 `unittest`/FastAPI already in the repository; existing vanilla `dashboard.html` and Node.js for a tiny renderer test. No new dependencies.

---

## Isolation and interfaces

Use a separate `.worktrees/` branch based on `master`. The main checkout's `dashboard.html`, `signal_server.py` and `signal_server_core.py` have unrelated uncommitted changes: preserve them, inspect before merging, and stop/ask if a merge overlaps. Do not push without a later user request. The SEC pilot is optional; the UI must work from the existing eleven-module source report alone. No network, scanner, IBKR, worker or order imports in the new publisher/reader. Public browser payload must never include raw provider URLs, headlines, article text, share-price rows, credentials or raw filings. No trade approval regardless of source values.

The saved snapshot schema is:

```json
{
  "schema_version": 1,
  "generated_at": "2026-10-05T11:00:00Z",
  "decision": "NO_TRADE",
  "order_approval": false,
  "model_calibrated": false,
  "target_probabilities": "unavailable",
  "sample": {"issuer_id": "issuer-1", "symbol": "SORA", "date": "2025-05-28"},
  "sources": {
    "roster": {"status": "UNVERIFIED", "active_count": 1, "inactive_count": 0},
    "news": {"status": "ARTICLES_OBSERVED", "article_count": 2},
    "ibkr": {"minute_cells": 1, "quote_cells": 1},
    "shares": {"status": "MARKET_CAP_UNVERIFIED"}
  },
  "blocking_reasons": ["MARKET_CAP_UNVERIFIED", "ROSTER_COVERAGE_UNVERIFIED"]
}
```

`generated_at` is publication time, not the historical decision time or a provider's fetch time. Exact values above illustrate shape only; counts must come from validated report fields, never hard-coded. The publisher always retains `MARKET_CAP_UNVERIFIED` and `ROSTER_COVERAGE_UNVERIFIED` while their approved gates remain unresolved. A saved SEC pilot may add a `SEC_*` blocker or observation but not change shares status to verified.

## Task 1: Snapshot projection and atomic publisher

**Files:** Create `microcap_readiness.py`, `test_microcap_readiness.py`.

- [ ] **Step 1: Write failing tests** for `project_readiness(report: Mapping, sec: Mapping | None, *, now: datetime) -> dict[str, object]` and `publish_readiness(report_path: Path, status_path: Path, sec_path: Path | None = None)`. Use real `extend_coverage` test-shaped output and check the exact allowlist above, `NO_TRADE`, counts from matrix/provider summaries, UTC generation time, sorted blockers and missing SEC status. Example:

  ```python
  result = project_readiness(report, None, now=datetime(2026, 10, 5, 11, tzinfo=timezone.utc))
  self.assertEqual(result["decision"], "NO_TRADE")
  self.assertEqual(result["sources"]["shares"]["status"], "MARKET_CAP_UNVERIFIED")
  self.assertNotIn("articles", json.dumps(result))
  ```

- [ ] **Step 2: Run** `./venv/bin/python -m unittest test_microcap_readiness -q`; expect missing-module failure.
- [ ] **Step 3: Implement** strict source report checks for schema version, safety fields, one declared issuer/date/symbol, matrix rows matching the manifest, named source statuses/counts and reason arrays. Map only exact known status/reason values to UI constants; reject unknown/extra type mismatches, do not serialize arbitrary provider strings. Do not treat a `"verified"` flag or optional saved SEC JSON as an audited-source witness. Bound input file bytes, require absolute external paths, use a same-directory temporary file + `os.replace` for atomic publish (clean up temp file on exceptions); preserve old snapshot on validation error. CLI `--source-report /external/source.json --output /external/readiness.json [--sec-report /external/sec.json]` exits 2 on invalid input and emits no sensitive raw data.
- [ ] **Step 4: Add tests** for forged approvals, unknown statuses, missing fields, wrong issuer/window, malicious credential-like article/source text, malformed/oversized source, atomic replacement failure, SEC evidence that claims verified, and zero/missing counts. Run targeted suite and commit just these two files with the co-author trailer.

## Task 2: Authenticated local snapshot endpoint

**Files:** Create `microcap_readiness_store.py`, `test_microcap_readiness_store.py`, `test_microcap_readiness_route.py`; modify `signal_server.py` only at the authenticated read-only route/import.

- [ ] **Step 1: Write failing tests** for `read_readiness(path: str | None, *, now: datetime) -> dict[str,object]`: missing path/file, invalid/symlinked repo path, oversize/invalid JSON, unsupported version, forged approval, future `generated_at`, >7-day-old snapshot, and valid status. Invalid/stale returns fixed `NO_TRADE` safety fields and an `UNAVAILABLE`/`STALE` status with fixed blocker, no old claims copied. Example:

  ```python
  result = read_readiness(None, now=datetime(2026, 10, 5, 11, tzinfo=timezone.utc))
  self.assertEqual(result["decision"], "NO_TRADE")
  self.assertEqual(result["status"], "UNAVAILABLE")
  ```

- [ ] **Step 2: Run** `./venv/bin/python -m unittest test_microcap_readiness_store -q`; expect missing-module failure.
- [ ] **Step 3: Implement** reader of only server-configured `MICROCAP_READINESS_PATH`, not a client path; `Path.resolve` must stay outside repository. Limit bytes and validate schema strictly; use `parse_utc` for timestamp and 7-day age, return fixed sanitized fallback on expected file/parse/validation errors (not a success-shaped empty report). Endpoint:

  ```python
  @app.get("/microcap-research-status")
  def microcap_research_status(user=Depends(dashboard_auth)):
      return read_readiness(os.environ.get("MICROCAP_READINESS_PATH"),
                            now=datetime.now(timezone.utc))
  ```

  Use existing imports/conventions in `signal_server.py`; no network calls or writes. Test unauthenticated HTTP denial and authenticated response with FastAPI dependency override or existing auth fixtures; do not weaken `dashboard_auth`.
- [ ] **Step 4: Run** `./venv/bin/python -m unittest test_microcap_readiness_store test_microcap_readiness_route -q`, then commit reader/tests/route with trailer.

## Task 3: Dashboard panel and operations

**Files:** Create `microcap_research_panel.js`, `test_microcap_research_panel.js`; modify `dashboard.html`, `OPERATIONS.md`.

- [ ] **Step 1: Write failing Node tests** using a fake element map for `renderMicrocapResearch(data, getElement)`; assert `NO TRADE` label, source counts, age/blockers, `STALE`/`UNAVAILABLE` state, and that malicious strings are never assigned as HTML. Mock `fetch` failure independently from live scanner refresh:

  ```js
  const nodes = Object.fromEntries(
    ["microcapDecision", "microcapBlockers"].map(id => [id, {textContent: ""}])
  );
  renderMicrocapResearch({decision:"NO_TRADE", status:"UNAVAILABLE"},
                         id => nodes[id]);
  if (nodes.microcapDecision.textContent !== "NO TRADE") throw Error("unsafe label");
  ```

- [ ] **Step 2: Run** `node test_microcap_research_panel.js`; expect missing-module/function failure.
- [ ] **Step 3: Implement** a standalone script exporting a pure renderer for Node and using browser `textContent` (not `innerHTML`) for data; fetch `/microcap-research-status` asynchronously after existing `refresh()` without joining its `Promise.all`, and turn HTTP/JSON failures into visible `UNAVAILABLE / NO TRADE`. Add a static dedicated `<section id="microcapResearchSection">` above the scanner insertion point (`hotTable` section) with title **Micro-cap Research · NO TRADE**, read-only status/counts, generated-at/age and blockers. Keep existing controls, scanner render and live status untouched; load script alongside existing dashboard scripts.
- [ ] **Step 4: Run** `node test_microcap_research_panel.js`, `./venv/bin/python -m unittest test_microcap_readiness test_microcap_readiness_store -q`, `node --check microcap_research_panel.js`, and `git diff --check` on task files. Update `OPERATIONS.md` with publisher usage, external path, seven-day stale rule and dashboard location; commit only files for this task with trailer.

## Final verification

Review each task for spec compliance then quality. Run the eleven-module offline micro-cap suite plus `test_microcap_readiness`, `test_microcap_readiness_store` and `test_microcap_readiness_route`, and `node test_microcap_research_panel.js`. If the SEC pilot plan has run, add its three offline modules. Verify malformed/missing snapshots do not break the existing dashboard refresh and that the route remains authenticated. Stop before merging if main checkout's unrelated dashboard/server edits conflict; reconcile with the user instead of discarding them. No research status can enable Phase 2 or Phase 3.
