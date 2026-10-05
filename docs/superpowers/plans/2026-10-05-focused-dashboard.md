# Focused Micro-cap Dashboard (Layout B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the cluttered dashboard with a professional page that holds only the micro-cap research view plus safety controls. It has a top bar (mode, NO TRADE, IBKR dot, kill switch) and sidebar tabs (Research, Positions, Orders, Health). The old page stays available at `/dashboard-legacy`.

**Architecture:**
- `dashboard.html` is static markup plus CSS.
- `dashboard_app.js` holds top bar, tabs, positions/orders/health renderers, the kill switch modal, the trading-mode switch and polling. It exports pure functions for Node tests.
- `microcap_research_panel.js` gains a schema 2 batch renderer.
- Rendering uses only `textContent` and `createElement`; there is no `innerHTML`.
- Two new authenticated routes in `signal_server.py` serve the legacy page and the app script. `/dashboard` stays in `signal_server_core.py` and keeps serving `dashboard.html`.

**Tech Stack:** Vanilla HTML/CSS/JS (no CDN), FastAPI `FileResponse`, Node 24 `node:assert` tests, Python `unittest` route tests.

**Spec:** `docs/superpowers/specs/2026-10-05-microcap-multi-sample-pilot-design.md` section 4.

**Depends on:** Plan A Task 6 (schema 2 snapshot) for real data. The renderers are fully testable without it.

**Global rules:**
- Use `textContent` only. Never `innerHTML`, `outerHTML`, `insertAdjacentHTML` or `document.write` in new code.
- No external URLs (`http://`, `https://`, `//cdn`) in the new HTML or JS.
- Any fetch failure renders `UNAVAILABLE` on that card only. The kill switch works even if every data fetch fails.
- Backend routes for removed sections stay untouched.
- `PY=/home/oferke/trading-bot/venv/bin/python`. Run from the worktree root.

## Data contracts (from the existing backend)

- `GET /dashboard-summary` → `system.{kill_switch, kill_switch_reason, live_ready}`, `broker.{tws_connected, snapshot_age_seconds, account, net_liquidation, daily_pnl}`, `positions.{managed,total,max_managed,max_total}`, `safety.blockers[]`.
- `GET /status` → `broker.tws_connected`, `broker.snapshot_age_seconds`.
- `GET /dashboard-portfolio` contains:
  - `managed_positions[]` and `legacy_positions[]`, each with `symbol, side, quantity, avg_cost, market_price, market_value, unrealized_pnl, daily_pnl, ownership, protection_complete`.
  - `open_orders[]`, each with `symbol, action, quantity, order_type, price, status, order_id, order_ref`.
  - Totals: `total_count, total_market_value, total_position_unrealized_pnl, managed_unprotected_count`.
- `GET /dashboard-components` → `total, healthy, unhealthy, critical_failures, components[]` with `component, critical, effective_healthy, healthy, fresh, age_seconds, detail`.
- `GET /trading-mode` → `mode, account, port`. `POST /trading-mode` takes `{"mode":"PAPER"|"LIVE"}`. LIVE needs two confirmations in the UI.
- `POST /control/kill-switch` takes `{"enabled":bool,"reason":str|null}`. Enabling requires a non-empty reason.
- `GET /microcap-research-status` returns schema 1, schema 2, or the fallback `{status:"UNAVAILABLE"|"STALE",…}`.

## File structure

| File | Responsibility |
|---|---|
| `dashboard_legacy.html` (new) | Verbatim copy of the current live `dashboard.html` |
| `dashboard.html` (replace) | Layout B markup and CSS; loads the two scripts |
| `dashboard_app.js` (new) | Top bar, tabs, positions/orders/health, kill modal, mode switch, polling |
| `microcap_research_panel.js` (modify) | Add schema 2 batch renderer; keep schema 1 renderer |
| `signal_server.py` (modify) | `GET /dashboard-legacy`, `GET /dashboard-app.js` |
| `test_dashboard_app.js` (new) | Node tests with a fake DOM |
| `test_microcap_research_panel.js` (modify) | Schema 2 tests |
| `test_microcap_readiness_route.py` (modify) | Route auth tests |
| `test_dashboard_layout.py` (new) | Static checks: required IDs, no external URLs, no `innerHTML` |

---

### Task 1: Preserve the legacy page and add routes

**Files:**
- Create: `dashboard_legacy.html`
- Modify: `signal_server.py` (next to the existing `/microcap-research-panel.js` route, around line 1562)
- Test: `test_microcap_readiness_route.py`

- [ ] **Step 1: Copy the live page**

```bash
cp /home/oferke/trading-bot/dashboard.html dashboard_legacy.html
grep -nE "AKIA|sk_live|ghp_|api[_-]?key[\"' ]*[:=][\"' ]*[A-Za-z0-9]{16,}|PASSWORD *[:=]" dashboard_legacy.html || echo "no secrets"
```

Expected: `no secrets`. If anything matches, stop and report; don't commit.

- [ ] **Step 2: Write the failing route tests**

Append to the test class in `test_microcap_readiness_route.py` (it already has `isolated_server()` and `request_raw()`):

```python
    def test_dashboard_legacy_and_app_script_require_auth(self):
        server, _ = isolated_server()
        for path, media in (("/dashboard-legacy", "text/html"),
                            ("/dashboard-app.js", "application/javascript")):
            with self.subTest(path=path):
                status, _, _ = request_raw(server.app, path)
                self.assertEqual(status, 401)
                status, body, content_type = request_raw(server.app, path, auth=True)
                self.assertEqual(status, 200)
                self.assertTrue(content_type.startswith(media))
                self.assertTrue(body.strip())
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `$PY -m unittest test_microcap_readiness_route`
Expected: FAIL with 404 for `/dashboard-legacy`. If `dashboard_app.js` doesn't exist yet, the route returns 500 or raises; that's fine at this step.

- [ ] **Step 4: Add the routes and a placeholder script**

In `signal_server.py`, directly after `microcap_research_panel_script`:

```python
@app.get("/dashboard-legacy")
def dashboard_legacy(user=Depends(dashboard_auth)):
    return FileResponse(
        Path(__file__).with_name("dashboard_legacy.html"),
        media_type="text/html",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/dashboard-app.js")
def dashboard_app_script(user=Depends(dashboard_auth)):
    return FileResponse(
        Path(__file__).with_name("dashboard_app.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )
```

Create a placeholder `dashboard_app.js` (Task 3 replaces it):

```javascript
"use strict";
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `$PY -m unittest test_microcap_readiness_route`
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add dashboard_legacy.html signal_server.py dashboard_app.js test_microcap_readiness_route.py
git commit -m "Preserve legacy dashboard and add authenticated app script route

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Note: the worktree's `signal_server.py` is the committed version. The live checkout has uncommitted edits, so the rollout (Task 6) hand-applies these two routes there.

---

### Task 2: Schema 2 batch renderer in `microcap_research_panel.js`

**Files:**
- Modify: `microcap_research_panel.js`
- Test: `test_microcap_research_panel.js`

Behavior:
- `renderMicrocapResearch(data, getElement, createElement?)`:
  - The decision is always `NO TRADE`. Status, generated time, age and blockers render as today, but `#microcapBlockers` gets class `blocker-text` (this fixes the oversized text).
  - If `data.schema_version === 2`:
    - Show `#microcapBatchView`, hide `#microcapSingleView`.
    - KPIs: `#microcapCov-<key>` = `"observed/total"` and `#microcapCovPct-<key>` = `"NN%"` (or `-`) for `ibkr_minute, ibkr_quotes, roster_dated, news_found, sec_shares`.
    - Meta: `#microcapBatchAsOf`, `#microcapBatchCount`, `#microcapBatchBias` (joined with ", ").
    - `#microcapSampleRows` is rebuilt with `createElement("tr")`/`("td")` rows: Date, Symbol, Move (`+52.3%`), IBKR 1m (`✓`/`✗`), Quotes (`✓`/`✗`), Roster, News (count or `-`), SEC (count), Errors (joined or `—`).
  - Otherwise: show the single view and hide the batch view. Existing schema 1 fields render exactly as today. For `UNAVAILABLE`/`STALE` with no `sample`, hide both views.
- `createElement` defaults to `document.createElement` when `document` exists. Without it, rows are skipped.
- `loadMicrocapResearch(fetchImpl, getElement, createElement?)` passes `createElement` through.

- [ ] **Step 1: Write the failing tests**

In `test_microcap_research_panel.js`:

1. Replace `elementMap` with a version whose nodes also carry `hidden` and `replaceChildren`:

```javascript
function fakeNode(tag) {
  return {
    tagName: tag, textContent: "", className: "initial", hidden: false, children: [],
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...nodes) { this.children = nodes; },
  };
}

function elementMap(ids) {
  const nodes = Object.fromEntries(ids.map(id => [id, fakeNode("div")]));
  return { nodes, getElement: id => nodes[id] || null, createElement: tag => fakeNode(tag) };
}

const batchIds = [
  "microcapBatchView", "microcapSingleView", "microcapBatchAsOf", "microcapBatchCount",
  "microcapBatchBias", "microcapSampleRows",
  ...["ibkr_minute", "ibkr_quotes", "roster_dated", "news_found", "sec_shares"]
    .flatMap(key => ["microcapCov-" + key, "microcapCovPct-" + key]),
];
```

2. In `testMaliciousStringsUseTextOnly`, the `innerHTML` own-property check still holds. Fake nodes have no `innerHTML` key.

3. Add these tests and call them from the runner IIFE:

```javascript
function batchSnapshot() {
  return {
    status: "CURRENT", schema_version: 2, generated_at: "2026-10-05T12:00:00Z", age_seconds: 75,
    decision: "NO_TRADE", order_approval: false,
    batch: { as_of: "2026-10-02", manifest_sha256: "a".repeat(64), sample_count: 2,
             rule: {}, bias: ["RUNNER_SCREEN_SELECTED", "SELECTION_USES_SAME_DAY_OUTCOME",
                              "UNVERIFIED_PILOT"] },
    coverage: {
      ibkr_minute: { observed: 2, total: 2 }, ibkr_quotes: { observed: 1, total: 2 },
      roster_dated: { observed: 0, total: 2 }, news_found: { observed: 1, total: 2 },
      sec_shares: { observed: 2, total: 2 },
    },
    samples: [
      { date: "2026-10-02", symbol: "SORA", issuer_id: "0002033515", move_pct: 52.3,
        ibkr_minute: true, ibkr_quotes: true, roster: "DATED_ROSTER_OBSERVED", news_count: 2,
        sec_observations: 2, stage_errors: [] },
      { date: "2026-10-01", symbol: "ABCD", issuer_id: "0000000002", move_pct: 31,
        ibkr_minute: true, ibkr_quotes: false, roster: "MISSING", news_count: null,
        sec_observations: 1, stage_errors: ["ibkr:PROVIDER_ERROR", "source:SKIPPED"] },
    ],
    blockers: ["MARKET_CAP_UNVERIFIED", "SAMPLE_INCOMPLETE"],
  };
}

async function testBatchSnapshotRendersKpisAndRows() {
  const { nodes, getElement, createElement } = elementMap([...ids, ...batchIds]);
  renderMicrocapResearch(batchSnapshot(), getElement, createElement);
  assert.equal(nodes.microcapBatchView.hidden, false);
  assert.equal(nodes.microcapSingleView.hidden, true);
  assert.equal(nodes["microcapCov-ibkr_minute"].textContent, "2/2");
  assert.equal(nodes["microcapCovPct-ibkr_quotes"].textContent, "50%");
  assert.equal(nodes["microcapCovPct-roster_dated"].textContent, "0%");
  assert.equal(nodes.microcapBatchCount.textContent, "2");
  assert.equal(nodes.microcapBatchAsOf.textContent, "2026-10-02");
  assert.match(nodes.microcapBatchBias.textContent, /SELECTION_USES_SAME_DAY_OUTCOME/);
  const rows = nodes.microcapSampleRows.children;
  assert.equal(rows.length, 2);
  assert.deepEqual(rows[0].children.map(cell => cell.textContent),
    ["2026-10-02", "SORA", "+52.3%", "✓", "✓", "DATED_ROSTER_OBSERVED", "2", "2", "—"]);
  assert.deepEqual(rows[1].children.map(cell => cell.textContent),
    ["2026-10-01", "ABCD", "+31.0%", "✓", "✗", "MISSING", "-", "1",
     "ibkr:PROVIDER_ERROR, source:SKIPPED"]);
  assert.equal(nodes.microcapBlockers.className, "blocker-text");
  assert.equal(nodes.microcapDecision.textContent, "NO TRADE");
}

async function testBatchGarbageAndXss() {
  const { nodes, getElement, createElement } = elementMap([...ids, ...batchIds]);
  const attack = "<img src=x onerror=alert(1)>";
  renderMicrocapResearch({
    schema_version: 2, status: "CURRENT", batch: { bias: [attack], sample_count: attack },
    coverage: { ibkr_minute: { observed: -1, total: 2 }, news_found: "bad" },
    samples: [{ symbol: attack, move_pct: "x", stage_errors: [attack] }, null, "bad"],
  }, getElement, createElement);
  assert.equal(nodes["microcapCov-ibkr_minute"].textContent, "-");
  assert.equal(nodes["microcapCovPct-news_found"].textContent, "-");
  assert.equal(nodes.microcapBatchCount.textContent, "-");
  const rows = nodes.microcapSampleRows.children;
  assert.equal(rows.length, 1);
  assert.equal(rows[0].children[1].textContent, attack);
  assert.equal(rows[0].children[2].textContent, "-");
}

async function testUnavailableHidesBothViews() {
  const { nodes, getElement, createElement } = elementMap([...ids, ...batchIds]);
  renderMicrocapResearch({ status: "UNAVAILABLE", blockers: ["READINESS_UNAVAILABLE"] },
    getElement, createElement);
  assert.equal(nodes.microcapBatchView.hidden, true);
  assert.equal(nodes.microcapSingleView.hidden, true);
}

async function testSchema1ShowsSingleView() {
  const { nodes, getElement, createElement } = elementMap([...ids, ...batchIds]);
  renderMicrocapResearch({ status: "CURRENT", sample: { symbol: "SORA" } }, getElement,
    createElement);
  assert.equal(nodes.microcapSingleView.hidden, false);
  assert.equal(nodes.microcapBatchView.hidden, true);
}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node test_microcap_research_panel.js`
Expected: AssertionError in `testBatchSnapshotRendersKpisAndRows` (`microcapBatchView.hidden` is still `false` but the count is `""`, or similar).

- [ ] **Step 3: Implement**

In `microcap_research_panel.js`, add these helpers after `statusClass`:

```javascript
  const COVERAGE_KEYS = ["ibkr_minute", "ibkr_quotes", "roster_dated", "news_found", "sec_shares"];

  function show(getElement, id, visible) {
    const element = getElement(id);
    if (element) {
      element.hidden = !visible;
    }
  }

  function ratio(value) {
    const entry = object(value);
    const observed = entry.observed;
    const total = entry.total;
    if (!Number.isInteger(observed) || !Number.isInteger(total) || observed < 0 || total < 0
        || observed > total) {
      return ["-", "-"];
    }
    const percent = total ? Math.round((observed / total) * 100) + "%" : "-";
    return [observed + "/" + total, percent];
  }

  function move(value) {
    return Number.isFinite(value) ? "+" + value.toFixed(1) + "%" : "-";
  }

  function flag(value) {
    if (value === true) {
      return ["✓", "ok"];
    }
    if (value === false) {
      return ["✗", "bad"];
    }
    return ["-", ""];
  }

  function cell(createElement, value, className) {
    const node = createElement("td");
    node.textContent = value;
    if (className) {
      node.className = className;
    }
    return node;
  }

  function sampleRow(createElement, sample) {
    const row = createElement("tr");
    const minute = flag(sample.ibkr_minute);
    const quotes = flag(sample.ibkr_quotes);
    const errors = Array.isArray(sample.stage_errors)
      ? sample.stage_errors.filter(item => typeof item === "string" && item.length)
      : [];
    [
      cell(createElement, text(sample.date)),
      cell(createElement, text(sample.symbol), "symbol"),
      cell(createElement, move(sample.move_pct), "num"),
      cell(createElement, minute[0], minute[1]),
      cell(createElement, quotes[0], quotes[1]),
      cell(createElement, text(sample.roster)),
      cell(createElement, count(sample.news_count), "num"),
      cell(createElement, count(sample.sec_observations), "num"),
      cell(createElement, errors.length ? errors.join(", ") : "—", errors.length ? "bad" : "muted"),
    ].forEach(node => row.appendChild(node));
    return row;
  }

  function defaultCreateElement() {
    return typeof document !== "undefined" ? tag => document.createElement(tag) : null;
  }

  function renderBatch(snapshot, getElement, createElement) {
    const batch = object(snapshot.batch);
    const coverage = object(snapshot.coverage);
    COVERAGE_KEYS.forEach(key => {
      const [value, percent] = ratio(coverage[key]);
      set(getElement, "microcapCov-" + key, value);
      set(getElement, "microcapCovPct-" + key, percent);
    });
    set(getElement, "microcapBatchAsOf", text(batch.as_of));
    set(getElement, "microcapBatchCount", count(batch.sample_count));
    set(getElement, "microcapBatchBias", Array.isArray(batch.bias)
      ? batch.bias.filter(item => typeof item === "string" && item.length).join(", ") || "-"
      : "-");
    const body = getElement("microcapSampleRows");
    if (body && createElement) {
      const samples = Array.isArray(snapshot.samples) ? snapshot.samples : [];
      body.replaceChildren(...samples
        .filter(item => item && typeof item === "object" && !Array.isArray(item))
        .slice(0, 50)
        .map(item => sampleRow(createElement, item)));
    }
  }
```

Then change `renderMicrocapResearch`:
- Its signature becomes `function renderMicrocapResearch(data, getElement, createElement)`.
- Its last line becomes `set(getElement, "microcapBlockers", blockers || "-", "blocker-text");`.
- Append this after that line:

```javascript
    const batchMode = snapshot.schema_version === 2;
    const hasSample = Object.keys(sample).length > 0;
    show(getElement, "microcapBatchView", batchMode);
    show(getElement, "microcapSingleView", !batchMode && hasSample);
    if (batchMode) {
      renderBatch(snapshot, getElement, createElement || defaultCreateElement());
    }
```

Change `loadMicrocapResearch(fetchImpl, getElement)` to `loadMicrocapResearch(fetchImpl, getElement, createElement)`, and pass `createElement` as the third argument in both `renderMicrocapResearch` calls inside it.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node test_microcap_research_panel.js`
Expected: `microcap_research_panel tests passed`

- [ ] **Step 5: Commit**

```bash
git add microcap_research_panel.js test_microcap_research_panel.js
git commit -m "Render schema 2 micro-cap batch coverage in research panel

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 3: `dashboard_app.js` — top bar, tabs, positions, orders, health, kill switch, mode

**Files:**
- Replace: `dashboard_app.js` (placeholder from Task 1)
- Create: `test_dashboard_app.js`

Exported API (via `module.exports`; the browser uses `window.TradingMaxDashboard`):
- `TABS`, `tabFromHash(hash)`, `ibkrState(status)` → `"up"|"down"|"unknown"` (unknown when `tws_connected` is not boolean or `snapshot_age_seconds` is not finite or > 60).
- `modeLabel(data)`, `killSwitchBody(enabled, reason)` (throws `REASON_REQUIRED` when enabling with a blank reason), `modeConfirmations(mode)`.
- Renderers: `renderTopBar(doc, {summary, status, mode})`, `renderPositions(doc, portfolio)`, `renderOrders(doc, portfolio)`, `renderHealth(doc, components, summary)`, `markUnavailable(doc, ids)`.
- `createApp({doc, fetchImpl, win, panel})` → `{start, selectTab, refreshTop, refreshActive, refreshResearch, openKill, confirmKill, closeKill, changeMode, state}`.

DOM IDs used (Task 4's HTML must define all of them):
- Top bar: `modeBadge`, `ibkrDot`, `ibkrLabel`, `killState`, `killButton`.
- Tabs: `nav-<tab>`, `tab-<tab>` for each tab.
- Positions: `posManagedCount`, `posTotalCount`, `posMarketValue`, `posUnrealized`, `posUnprotected`, `managedRows`, `brokerRows`.
- Orders: `ordersCount`, `orderRows`.
- Health: `healthSummary`, `componentRows`, `safetyBlockers`, `safetyKillReason`, `modeText`, `modeAccount`, `modePaperBtn`, `modeLiveBtn`, `modeMessage`.
- Kill modal: `killModal`, `killModalTitle`, `killModalText`, `killReason`, `killError`, `killCancel`, `killConfirm`.

- [ ] **Step 1: Write the failing tests**

Create `test_dashboard_app.js`:

```javascript
const assert = require("node:assert/strict");
const fs = require("node:fs");
const app = require("./dashboard_app.js");

function fakeNode(tag) {
  return {
    tagName: tag, textContent: "", className: "", hidden: false, value: "", disabled: false,
    colSpan: 1, children: [], listeners: {},
    appendChild(child) { this.children.push(child); return child; },
    replaceChildren(...nodes) { this.children = nodes; },
    addEventListener(type, handler) { this.listeners[type] = handler; },
    focus() {},
  };
}

function fakeDoc() {
  const nodes = {};
  return {
    nodes,
    getElementById(id) { return nodes[id] || (nodes[id] = fakeNode("div")); },
    createElement: tag => fakeNode(tag),
  };
}

function cells(row) { return row.children.map(cell => cell.textContent); }

function response(body, ok = true) {
  return { ok, json: async () => body };
}

const ATTACK = "<img src=x onerror=alert(1)>";

function testPureHelpers() {
  assert.equal(app.tabFromHash("#orders"), "orders");
  assert.equal(app.tabFromHash("#nope"), "research");
  assert.equal(app.tabFromHash(undefined), "research");
  assert.equal(app.ibkrState({ broker: { tws_connected: true, snapshot_age_seconds: 4 } }), "up");
  assert.equal(app.ibkrState({ broker: { tws_connected: false, snapshot_age_seconds: 4 } }), "down");
  assert.equal(app.ibkrState({ broker: { tws_connected: true, snapshot_age_seconds: 61 } }), "unknown");
  assert.equal(app.ibkrState({ broker: { tws_connected: true, snapshot_age_seconds: null } }), "unknown");
  assert.equal(app.ibkrState(null), "unknown");
  assert.equal(app.modeLabel({ mode: "LIVE" }), "LIVE");
  assert.equal(app.modeLabel({ mode: ATTACK }), "UNKNOWN");
  assert.deepEqual(app.killSwitchBody(true, "  halt  "), { enabled: true, reason: "halt" });
  assert.deepEqual(app.killSwitchBody(false, " "), { enabled: false, reason: null });
  assert.throws(() => app.killSwitchBody(true, "   "), /REASON_REQUIRED/);
  assert.equal(app.modeConfirmations("PAPER").length, 1);
  assert.equal(app.modeConfirmations("LIVE").length, 2);
  assert.throws(() => app.modeConfirmations("YOLO"));
}

function testTopBar() {
  const doc = fakeDoc();
  app.renderTopBar(doc, {
    summary: { system: { kill_switch: true } },
    status: { broker: { tws_connected: true, snapshot_age_seconds: 3 } },
    mode: { mode: "PAPER" },
  });
  assert.equal(doc.nodes.modeBadge.textContent, "PAPER");
  assert.match(doc.nodes.modeBadge.className, /paper/);
  assert.match(doc.nodes.ibkrDot.className, /up/);
  assert.equal(doc.nodes.killState.textContent, "KILL SWITCH ON");
  assert.equal(doc.nodes.killButton.textContent, "Disable kill switch");
  app.renderTopBar(doc, { summary: null, status: null, mode: null });
  assert.equal(doc.nodes.modeBadge.textContent, "MODE ?");
  assert.match(doc.nodes.ibkrDot.className, /unknown/);
  assert.equal(doc.nodes.killState.textContent, "KILL SWITCH ?");
  assert.equal(doc.nodes.killButton.textContent, "Enable kill switch");
}

function testPositionsAndOrders() {
  const doc = fakeDoc();
  const portfolio = {
    managed_positions: [{ symbol: "SORA", side: "LONG", quantity: 100, avg_cost: 2.5,
      market_price: 3, market_value: 300, unrealized_pnl: 50, ownership: "MANAGED",
      protection_complete: false }],
    legacy_positions: [{ symbol: ATTACK, side: "LONG", quantity: 5, avg_cost: 10,
      market_price: 9, market_value: 45, unrealized_pnl: -5, ownership: "LEGACY" }, null],
    open_orders: [{ symbol: "SORA", action: "SELL", quantity: 100, order_type: "STP",
      price: 2.1, status: "Submitted", order_id: 42, order_ref: "tm-1" }],
    total_count: 2, total_market_value: 345, total_position_unrealized_pnl: 45,
    managed_unprotected_count: 1,
  };
  app.renderPositions(doc, portfolio);
  assert.equal(doc.nodes.posManagedCount.textContent, "1");
  assert.equal(doc.nodes.posTotalCount.textContent, "2");
  assert.equal(doc.nodes.posMarketValue.textContent, "345.00");
  assert.equal(doc.nodes.posUnrealized.textContent, "+45.00");
  assert.equal(doc.nodes.posUnprotected.textContent, "1");
  assert.match(doc.nodes.posUnprotected.className, /bad/);
  assert.deepEqual(cells(doc.nodes.managedRows.children[0]),
    ["SORA", "LONG", "100", "2.50", "3.00", "300.00", "+50.00", "UNPROTECTED"]);
  assert.equal(doc.nodes.brokerRows.children.length, 2);
  assert.equal(cells(doc.nodes.brokerRows.children[1])[0], ATTACK);
  assert.equal(cells(doc.nodes.brokerRows.children[1])[1], "LEGACY");

  app.renderOrders(doc, portfolio);
  assert.equal(doc.nodes.ordersCount.textContent, "1");
  assert.deepEqual(cells(doc.nodes.orderRows.children[0]),
    ["SORA", "SELL", "100", "STP", "2.10", "Submitted", "42", "tm-1"]);

  app.renderPositions(doc, {});
  assert.equal(cells(doc.nodes.managedRows.children[0])[0], "No managed positions");
  app.renderOrders(doc, {});
  assert.equal(cells(doc.nodes.orderRows.children[0])[0], "No open orders");
}

function testHealth() {
  const doc = fakeDoc();
  app.renderHealth(doc, {
    total: 3, healthy: 1, critical_failures: 1,
    components: [
      { component: "worker", critical: true, effective_healthy: true, healthy: true, fresh: true, age_seconds: 4.7, detail: "ok" },
      { component: "scanner", critical: false, effective_healthy: false, healthy: true, fresh: false, age_seconds: 900, detail: "old" },
      { component: "broker", critical: true, effective_healthy: false, healthy: false, fresh: true, age_seconds: null, detail: ATTACK },
    ],
  }, { system: { kill_switch_reason: "manual" }, safety: { blockers: ["MAX_POSITIONS"] } });
  assert.equal(doc.nodes.healthSummary.textContent, "1/3 healthy · 1 critical failures");
  assert.deepEqual(doc.nodes.componentRows.children.map(row => cells(row)[1]),
    ["HEALTHY", "STALE", "UNHEALTHY"]);
  assert.equal(cells(doc.nodes.componentRows.children[0])[3], "4s ago");
  assert.equal(cells(doc.nodes.componentRows.children[2])[4], ATTACK);
  assert.deepEqual(doc.nodes.safetyBlockers.children.map(item => item.textContent), ["MAX_POSITIONS"]);
  assert.equal(doc.nodes.safetyKillReason.textContent, "manual");
  app.renderHealth(doc, {}, {});
  assert.deepEqual(doc.nodes.safetyBlockers.children.map(item => item.textContent), ["No safety blockers"]);
}

function harness(routes, confirms = []) {
  const doc = fakeDoc();
  const calls = [];
  const fetchImpl = async (url, options = {}) => {
    calls.push({ url, method: options.method || "GET", body: options.body });
    const route = routes[url];
    if (route instanceof Error) { throw route; }
    return response(route === undefined ? {} : route);
  };
  const win = { location: { hash: "" }, addEventListener() {}, setInterval() {},
    confirm: () => (confirms.length ? confirms.shift() : true) };
  const panel = { loadMicrocapResearch: async () => {} };
  return { doc, calls, instance: app.createApp({ doc, fetchImpl, win, panel }) };
}

async function testKillSwitchFlow() {
  const { doc, calls, instance } = harness({ "/dashboard-summary": { system: { kill_switch: false } } });
  await instance.refreshTop();
  instance.openKill();
  assert.equal(doc.nodes.killModal.hidden, false);
  assert.equal(doc.nodes.killConfirm.textContent, "ENABLE");
  doc.nodes.killReason.value = "  ";
  await instance.confirmKill();
  assert.equal(calls.filter(call => call.method === "POST").length, 0);
  assert.match(doc.nodes.killError.textContent, /reason is required/);
  doc.nodes.killReason.value = "news halt";
  await instance.confirmKill();
  const post = calls.find(call => call.method === "POST");
  assert.equal(post.url, "/control/kill-switch");
  assert.deepEqual(JSON.parse(post.body), { enabled: true, reason: "news halt" });
  assert.equal(doc.nodes.killModal.hidden, true);
}

async function testKillSwitchWorksWhenDataFails() {
  const { doc, calls, instance } = harness({
    "/dashboard-summary": new Error("down"), "/status": new Error("down"),
    "/trading-mode": new Error("down"),
  });
  await instance.refreshTop();
  instance.openKill();
  doc.nodes.killReason.value = "emergency";
  await instance.confirmKill();
  assert.ok(calls.some(call => call.method === "POST" && call.url === "/control/kill-switch"));
}

async function testModeSwitchNeedsBothConfirmations() {
  const declined = harness({}, [true, false]);
  await declined.instance.changeMode("LIVE");
  assert.equal(declined.calls.filter(call => call.method === "POST").length, 0);
  const accepted = harness({ "/trading-mode": { mode: "LIVE", account: "U1", port: "7496" } },
    [true, true]);
  await accepted.instance.changeMode("LIVE");
  const post = accepted.calls.find(call => call.method === "POST");
  assert.deepEqual(JSON.parse(post.body), { mode: "LIVE" });
  assert.equal(accepted.doc.nodes.modeMessage.textContent, "LIVE enabled");
}

async function testTabsAndUnavailable() {
  const { doc, instance } = harness({ "/dashboard-portfolio": new Error("down") });
  instance.selectTab("positions");
  assert.equal(doc.nodes["tab-positions"].hidden, false);
  assert.equal(doc.nodes["tab-research"].hidden, true);
  assert.match(doc.nodes["nav-positions"].className, /active/);
  await instance.refreshActive();
  assert.equal(doc.nodes.posTotalCount.textContent, "UNAVAILABLE");
}

function testNoUnsafeHtmlSinks() {
  const source = fs.readFileSync(require.resolve("./dashboard_app.js"), "utf8");
  assert.doesNotMatch(source, /innerHTML|outerHTML|insertAdjacentHTML|document\.write|https?:\/\//);
}

(async () => {
  testPureHelpers();
  testTopBar();
  testPositionsAndOrders();
  testHealth();
  await testKillSwitchFlow();
  await testKillSwitchWorksWhenDataFails();
  await testModeSwitchNeedsBothConfirmations();
  await testTabsAndUnavailable();
  testNoUnsafeHtmlSinks();
  console.log("dashboard_app tests passed");
})().catch(error => {
  console.error(error);
  process.exit(1);
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `node test_dashboard_app.js`
Expected: `TypeError: app.tabFromHash is not a function`

- [ ] **Step 3: Implement `dashboard_app.js`**

Polling:
- Every 5 s: the top bar (summary, status) and the active tab.
- Every 30 s: the trading mode. `GET /trading-mode` runs a privileged helper, so it is not polled every 5 s.
- Every 60 s: research.
- Overlapping refreshes are skipped.

```javascript
(function (root) {
  "use strict";

  const TABS = ["research", "positions", "orders", "health"];
  const IBKR_MAX_AGE_SECONDS = 60;
  const POLL_MS = 5000;
  const MODE_POLL_MS = 30000;
  const RESEARCH_POLL_MS = 60000;
  const POSITION_IDS = ["posManagedCount", "posTotalCount", "posMarketValue", "posUnrealized",
    "posUnprotected"];

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function list(value) {
    return Array.isArray(value) ? value.filter(item => item && typeof item === "object"
      && !Array.isArray(item)) : [];
  }

  function str(value) {
    return typeof value === "string" && value.length ? value : "-";
  }

  function num(value, digits = 2) {
    return Number.isFinite(value)
      ? value.toLocaleString("en-US", { minimumFractionDigits: digits, maximumFractionDigits: digits })
      : "-";
  }

  function signed(value) {
    return Number.isFinite(value) ? (value > 0 ? "+" : "") + num(value) : "-";
  }

  function pnlClass(value) {
    return Number.isFinite(value) ? (value > 0 ? "pos" : value < 0 ? "neg" : "") : "";
  }

  function plain(value) {
    return Number.isFinite(value) ? String(value) : "-";
  }

  function tabFromHash(hash) {
    const name = typeof hash === "string" ? hash.replace(/^#/, "") : "";
    return TABS.includes(name) ? name : "research";
  }

  function ibkrState(status) {
    const broker = object(object(status).broker);
    const age = broker.snapshot_age_seconds;
    if (typeof broker.tws_connected !== "boolean" || !Number.isFinite(age) || age < 0
        || age > IBKR_MAX_AGE_SECONDS) {
      return "unknown";
    }
    return broker.tws_connected ? "up" : "down";
  }

  function modeLabel(data) {
    const mode = object(data).mode;
    return mode === "PAPER" || mode === "LIVE" ? mode : "UNKNOWN";
  }

  function killSwitchBody(enabled, reason) {
    const trimmed = typeof reason === "string" ? reason.trim() : "";
    if (enabled && !trimmed) {
      throw new Error("REASON_REQUIRED");
    }
    return { enabled: Boolean(enabled), reason: trimmed || null };
  }

  function modeConfirmations(mode) {
    if (mode === "PAPER") {
      return ["Switch to PAPER TRADING?"];
    }
    if (mode === "LIVE") {
      return ["Switch to LIVE MONEY trading?\n\nReal orders will be sent to IBKR.",
        "SECOND CONFIRMATION:\nEnable REAL MONEY trading?"];
    }
    throw new Error("MODE_INVALID");
  }

  function setText(doc, id, value, className) {
    const element = doc.getElementById(id);
    if (!element) {
      return;
    }
    element.textContent = value;
    if (className !== undefined) {
      element.className = className;
    }
  }

  function fillRows(doc, id, rows, emptyText, columns) {
    const body = doc.getElementById(id);
    if (!body) {
      return;
    }
    const nodes = rows.map(values => {
      const row = doc.createElement("tr");
      values.forEach(([value, className]) => {
        const cell = doc.createElement("td");
        cell.textContent = value;
        if (className) {
          cell.className = className;
        }
        row.appendChild(cell);
      });
      return row;
    });
    if (!nodes.length) {
      const row = doc.createElement("tr");
      const cell = doc.createElement("td");
      cell.colSpan = columns;
      cell.className = "empty";
      cell.textContent = emptyText;
      row.appendChild(cell);
      nodes.push(row);
    }
    body.replaceChildren(...nodes);
  }

  function fillList(doc, id, items, emptyText) {
    const target = doc.getElementById(id);
    if (!target) {
      return;
    }
    const values = items.length ? items : [emptyText];
    target.replaceChildren(...values.map(value => {
      const item = doc.createElement("li");
      item.textContent = value;
      item.className = items.length ? "warn" : "muted";
      return item;
    }));
  }

  function markUnavailable(doc, ids) {
    ids.forEach(id => setText(doc, id, "UNAVAILABLE", "muted"));
  }

  function renderTopBar(doc, { summary, status, mode }) {
    const label = modeLabel(mode);
    setText(doc, "modeBadge", label === "UNKNOWN" ? "MODE ?" : label,
      "badge " + label.toLowerCase());
    const state = ibkrState(status);
    setText(doc, "ibkrDot", "", "dot " + state);
    setText(doc, "ibkrLabel", { up: "IBKR connected", down: "IBKR disconnected",
      unknown: "IBKR unknown" }[state]);
    const kill = object(object(summary).system).kill_switch;
    if (kill === true) {
      setText(doc, "killState", "KILL SWITCH ON", "badge danger");
      setText(doc, "killButton", "Disable kill switch");
    } else {
      setText(doc, "killState", kill === false ? "KILL SWITCH OFF" : "KILL SWITCH ?",
        kill === false ? "badge ok" : "badge unknown");
      setText(doc, "killButton", "Enable kill switch");
    }
    return typeof kill === "boolean" ? kill : null;
  }

  function renderModePanel(doc, mode) {
    const label = modeLabel(mode);
    const data = object(mode);
    setText(doc, "modeText", { PAPER: "PAPER TRADING", LIVE: "LIVE MONEY" }[label] || "UNAVAILABLE",
      "mode-text " + label.toLowerCase());
    setText(doc, "modeAccount", str(data.account) + " · TWS :" + str(data.port));
  }

  function protection(position) {
    if (String(position.ownership || "").toUpperCase() !== "MANAGED") {
      return ["-", "muted"];
    }
    return position.protection_complete === true ? ["PROTECTED", "ok"] : ["UNPROTECTED", "bad"];
  }

  function renderPositions(doc, portfolio) {
    const data = object(portfolio);
    const managed = list(data.managed_positions);
    const legacy = list(data.legacy_positions);
    const unprotected = data.managed_unprotected_count;
    setText(doc, "posManagedCount", String(managed.length));
    setText(doc, "posTotalCount", Number.isInteger(data.total_count)
      ? String(data.total_count) : String(managed.length + legacy.length));
    setText(doc, "posMarketValue", num(data.total_market_value));
    setText(doc, "posUnrealized", signed(data.total_position_unrealized_pnl),
      "kpi-value " + pnlClass(data.total_position_unrealized_pnl));
    setText(doc, "posUnprotected", plain(unprotected),
      "kpi-value " + (Number.isInteger(unprotected) && unprotected > 0 ? "bad" : "ok"));
    fillRows(doc, "managedRows", managed.map(p => [
      [str(p.symbol), "symbol"], [str(p.side)], [plain(p.quantity), "num"],
      [num(p.avg_cost), "num"], [num(p.market_price), "num"], [num(p.market_value), "num"],
      [signed(p.unrealized_pnl), "num " + pnlClass(p.unrealized_pnl)], protection(p),
    ]), "No managed positions", 8);
    fillRows(doc, "brokerRows", [...managed, ...legacy].map(p => [
      [str(p.symbol), "symbol"], [str(String(p.ownership || "").toUpperCase())], [str(p.side)],
      [plain(p.quantity), "num"], [num(p.avg_cost), "num"], [num(p.market_price), "num"],
      [num(p.market_value), "num"], [signed(p.unrealized_pnl), "num " + pnlClass(p.unrealized_pnl)],
    ]), "No broker positions", 8);
  }

  function renderOrders(doc, portfolio) {
    const orders = list(object(portfolio).open_orders);
    setText(doc, "ordersCount", String(orders.length));
    fillRows(doc, "orderRows", orders.map(o => [
      [str(o.symbol), "symbol"], [str(o.action)], [plain(o.quantity), "num"], [str(o.order_type)],
      [num(o.price), "num"], [str(o.status)], [plain(o.order_id), "num"], [str(o.order_ref)],
    ]), "No open orders", 8);
  }

  function componentStatus(component) {
    if (component.effective_healthy === true) {
      return ["HEALTHY", "ok"];
    }
    if (component.healthy === true && component.fresh === false) {
      return ["STALE", "warn"];
    }
    return ["UNHEALTHY", "bad"];
  }

  function renderHealth(doc, components, summary) {
    const runtime = object(components);
    const failures = runtime.critical_failures;
    setText(doc, "healthSummary",
      plain(runtime.healthy) + "/" + plain(runtime.total) + " healthy · "
        + plain(failures) + " critical failures",
      Number.isInteger(failures) && failures > 0 ? "summary bad" : "summary ok");
    fillRows(doc, "componentRows", list(runtime.components).map(c => [
      [str(c.component)], componentStatus(c),
      [c.critical === true ? "CRITICAL" : "OPTIONAL", c.critical === true ? "" : "muted"],
      [Number.isFinite(c.age_seconds) && c.age_seconds >= 0
        ? Math.floor(c.age_seconds) + "s ago" : "-", "num"],
      [str(c.detail), "detail"],
    ]), "No runtime component data", 5);
    const data = object(summary);
    const blockers = Array.isArray(object(data.safety).blockers)
      ? data.safety.blockers.filter(item => typeof item === "string" && item.length) : [];
    fillList(doc, "safetyBlockers", blockers, "No safety blockers");
    setText(doc, "safetyKillReason", str(object(data.system).kill_switch_reason));
  }

  async function fetchJson(fetchImpl, url, options) {
    const reply = await fetchImpl(url, Object.assign(
      { credentials: "same-origin", cache: "no-store" }, options || {}));
    if (!reply || reply.ok !== true) {
      throw new Error("HTTP_ERROR");
    }
    return reply.json();
  }

  function createApp({ doc, fetchImpl, win, panel }) {
    const state = { tab: "research", killOn: null, killTarget: true, mode: null,
      summary: null, busy: {} };

    function guarded(name, task) {
      return async () => {
        if (state.busy[name]) {
          return;
        }
        state.busy[name] = true;
        try {
          await task();
        } finally {
          state.busy[name] = false;
        }
      };
    }

    function selectTab(name) {
      state.tab = tabFromHash(name);
      TABS.forEach(tab => {
        const panelNode = doc.getElementById("tab-" + tab);
        if (panelNode) {
          panelNode.hidden = tab !== state.tab;
        }
        setText(doc, "nav-" + tab, (doc.getElementById("nav-" + tab) || {}).textContent || tab,
          tab === state.tab ? "tab active" : "tab");
      });
    }

    async function loadMode() {
      try {
        state.mode = await fetchJson(fetchImpl, "/trading-mode");
      } catch (error) {
        state.mode = null;
      }
      renderModePanel(doc, state.mode);
    }

    async function refreshTop() {
      const [summary, status] = await Promise.allSettled([
        fetchJson(fetchImpl, "/dashboard-summary"), fetchJson(fetchImpl, "/status")]);
      if (state.mode === null) {
        await loadMode();
      }
      state.summary = summary.status === "fulfilled" ? summary.value : null;
      state.killOn = renderTopBar(doc, {
        summary: state.summary,
        status: status.status === "fulfilled" ? status.value : null,
        mode: state.mode,
      });
    }

    async function refreshActive() {
      if (state.tab === "positions" || state.tab === "orders") {
        try {
          const portfolio = await fetchJson(fetchImpl, "/dashboard-portfolio");
          renderPositions(doc, portfolio);
          renderOrders(doc, portfolio);
        } catch (error) {
          markUnavailable(doc, [...POSITION_IDS, "ordersCount"]);
          fillRows(doc, "managedRows", [], "UNAVAILABLE", 8);
          fillRows(doc, "brokerRows", [], "UNAVAILABLE", 8);
          fillRows(doc, "orderRows", [], "UNAVAILABLE", 8);
        }
      } else if (state.tab === "health") {
        try {
          renderHealth(doc, await fetchJson(fetchImpl, "/dashboard-components"), state.summary);
        } catch (error) {
          markUnavailable(doc, ["healthSummary"]);
          fillRows(doc, "componentRows", [], "UNAVAILABLE", 5);
        }
        renderModePanel(doc, state.mode);
      }
    }

    async function refreshResearch() {
      if (panel && typeof panel.loadMicrocapResearch === "function") {
        await panel.loadMicrocapResearch(
          (url, options) => fetchImpl(url, Object.assign({ credentials: "same-origin",
            cache: "no-store" }, options || {})),
          id => doc.getElementById(id), tag => doc.createElement(tag));
      }
    }

    function openKill() {
      state.killTarget = state.killOn !== true;
      setText(doc, "killModalTitle", state.killTarget ? "Enable kill switch" : "Disable kill switch");
      setText(doc, "killModalText", state.killTarget
        ? "This blocks creation of new LIVE signals. Existing broker positions are not automatically closed."
        : "This removes the TradingMax kill switch. All normal safety gates remain active.");
      setText(doc, "killConfirm", state.killTarget ? "ENABLE" : "DISABLE");
      setText(doc, "killError", "");
      const reason = doc.getElementById("killReason");
      if (reason) {
        reason.value = "";
      }
      const modal = doc.getElementById("killModal");
      if (modal) {
        modal.hidden = false;
      }
      if (reason && typeof reason.focus === "function") {
        reason.focus();
      }
    }

    function closeKill() {
      const modal = doc.getElementById("killModal");
      if (modal) {
        modal.hidden = true;
      }
    }

    async function confirmKill() {
      let body;
      try {
        body = killSwitchBody(state.killTarget, (doc.getElementById("killReason") || {}).value);
      } catch (error) {
        setText(doc, "killError", "A reason is required to enable the kill switch.");
        return;
      }
      const button = doc.getElementById("killConfirm");
      if (button) {
        button.disabled = true;
      }
      try {
        await fetchJson(fetchImpl, "/control/kill-switch", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        closeKill();
        await refreshTop();
      } catch (error) {
        setText(doc, "killError", "Kill switch request failed. Try again.");
      } finally {
        if (button) {
          button.disabled = false;
        }
      }
    }

    async function changeMode(mode) {
      let prompts;
      try {
        prompts = modeConfirmations(mode);
      } catch (error) {
        return;
      }
      for (const prompt of prompts) {
        if (!win.confirm(prompt)) {
          return;
        }
      }
      const buttons = ["modePaperBtn", "modeLiveBtn"].map(id => doc.getElementById(id))
        .filter(Boolean);
      buttons.forEach(button => { button.disabled = true; });
      setText(doc, "modeMessage", "Switching…");
      try {
        await fetchJson(fetchImpl, "/trading-mode", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ mode }),
        });
        setText(doc, "modeMessage", mode + " enabled");
      } catch (error) {
        setText(doc, "modeMessage", "Mode switch failed");
      } finally {
        buttons.forEach(button => { button.disabled = false; });
        await loadMode();
        await refreshTop();
      }
    }

    function on(id, handler) {
      const element = doc.getElementById(id);
      if (element && typeof element.addEventListener === "function") {
        element.addEventListener("click", handler);
      }
    }

    function start() {
      on("killButton", openKill);
      on("killCancel", closeKill);
      on("killConfirm", () => { confirmKill(); });
      on("modePaperBtn", () => { changeMode("PAPER"); });
      on("modeLiveBtn", () => { changeMode("LIVE"); });
      const pollTop = guarded("top", refreshTop);
      const pollActive = guarded("active", refreshActive);
      const pollResearch = guarded("research", refreshResearch);
      win.addEventListener("hashchange", () => {
        selectTab(win.location.hash);
        pollActive();
      });
      selectTab(win.location.hash);
      loadMode().then(pollTop);
      pollActive();
      pollResearch();
      win.setInterval(() => { pollTop(); pollActive(); }, POLL_MS);
      win.setInterval(loadMode, MODE_POLL_MS);
      win.setInterval(pollResearch, RESEARCH_POLL_MS);
    }

    return { start, selectTab, refreshTop, refreshActive, refreshResearch, openKill,
      closeKill, confirmKill, changeMode, state };
  }

  const api = { TABS, tabFromHash, ibkrState, modeLabel, killSwitchBody, modeConfirmations,
    renderTopBar, renderPositions, renderOrders, renderHealth, markUnavailable, createApp };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.TradingMaxDashboard = api;
  if (typeof document !== "undefined" && typeof window !== "undefined") {
    document.addEventListener("DOMContentLoaded", () => {
      createApp({ doc: document, fetchImpl: window.fetch.bind(window), win: window,
        panel: window }).start();
    });
  }
})(typeof window !== "undefined" ? window : globalThis);
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `node test_dashboard_app.js && node test_microcap_research_panel.js`
Expected: `dashboard_app tests passed` and `microcap_research_panel tests passed`.

- [ ] **Step 5: Commit**

```bash
git add dashboard_app.js test_dashboard_app.js
git commit -m "Add focused dashboard app: top bar, tabs, positions, orders, health, kill switch

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 4: New `dashboard.html` (layout B) and static layout test

**Files:**
- Replace: `dashboard.html`
- Create: `test_dashboard_layout.py`

- [ ] **Step 1: Write the failing static test**

Create `test_dashboard_layout.py`:

```python
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path


ROOT = Path(__file__).resolve().parent
REQUIRED_IDS = {
    "modeBadge", "ibkrDot", "ibkrLabel", "killState", "killButton",
    "nav-research", "nav-positions", "nav-orders", "nav-health",
    "tab-research", "tab-positions", "tab-orders", "tab-health",
    "microcapDecision", "microcapStatus", "microcapGeneratedAt", "microcapAge",
    "microcapBlockers", "microcapBatchView", "microcapSingleView", "microcapBatchAsOf",
    "microcapBatchCount", "microcapBatchBias", "microcapSampleRows",
    "microcapSampleSymbol", "microcapSampleDate", "microcapRosterStatus",
    "microcapRosterCounts", "microcapNewsStatus", "microcapNewsCount",
    "microcapIbkrMinuteCells", "microcapIbkrQuoteCells", "microcapSecStatus",
    "microcapSharesStatus",
    *(f"microcapCov-{key}" for key in ("ibkr_minute", "ibkr_quotes", "roster_dated",
                                       "news_found", "sec_shares")),
    *(f"microcapCovPct-{key}" for key in ("ibkr_minute", "ibkr_quotes", "roster_dated",
                                          "news_found", "sec_shares")),
    "posManagedCount", "posTotalCount", "posMarketValue", "posUnrealized", "posUnprotected",
    "managedRows", "brokerRows", "ordersCount", "orderRows",
    "healthSummary", "componentRows", "safetyBlockers", "safetyKillReason",
    "modeText", "modeAccount", "modePaperBtn", "modeLiveBtn", "modeMessage",
    "killModal", "killModalTitle", "killModalText", "killReason", "killError",
    "killCancel", "killConfirm",
}
REMOVED_MARKERS = ("Strategy Pipeline", "Top Gainers", "Scanner Universe", "Hot Pool",
                   "Trading Day", "Systemd Services", "Recent Signals", "Event Stream",
                   "strategy-mode", "strategyMode")


class _Collector(HTMLParser):
    def __init__(self):
        super().__init__()
        self.ids, self.scripts, self.hidden = [], [], set()

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if "id" in values:
            self.ids.append(values["id"])
            if "hidden" in values:
                self.hidden.add(values["id"])
        if tag == "script":
            self.scripts.append(values.get("src"))


class DashboardLayoutTests(unittest.TestCase):
    def setUp(self):
        self.html = (ROOT / "dashboard.html").read_text(encoding="utf-8")
        self.parsed = _Collector()
        self.parsed.feed(self.html)

    def test_required_ids_exist_once(self):
        self.assertEqual(REQUIRED_IDS - set(self.parsed.ids), set())
        duplicates = {i for i in self.parsed.ids if self.parsed.ids.count(i) > 1}
        self.assertEqual(duplicates, set())

    def test_only_local_scripts_and_no_external_urls(self):
        self.assertEqual(self.parsed.scripts,
                         ["/microcap-research-panel.js", "/dashboard-app.js"])
        self.assertIsNone(re.search(r"https?://|//cdn|innerHTML", self.html))

    def test_removed_sections_are_gone(self):
        for marker in REMOVED_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, self.html)

    def test_initial_visibility(self):
        self.assertIn("killModal", self.parsed.hidden)
        for tab in ("tab-positions", "tab-orders", "tab-health"):
            self.assertIn(tab, self.parsed.hidden)
        self.assertNotIn("tab-research", self.parsed.hidden)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `$PY -m unittest test_dashboard_layout`
Expected: FAIL. The old page lacks the required IDs and still contains the removed markers.

- [ ] **Step 3: Write the new `dashboard.html`**

```html
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TradingMax · Micro-cap</title>
<style>
:root{--bg:#070a0f;--panel:#0f141d;--panel2:#151b26;--border:#242c39;--text:#e9eef6;--muted:#8490a3;--green:#39d98a;--red:#ff626d;--orange:#ffb65c;--blue:#63a9ff;--shadow:0 8px 28px rgba(0,0,0,.25)}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--text);font:14px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
[hidden]{display:none!important}
.topbar{position:sticky;top:0;z-index:5;display:flex;align-items:center;gap:12px;padding:10px 20px;background:var(--panel);border-bottom:1px solid var(--border)}
.brand{font-weight:700;letter-spacing:.3px;margin-right:auto}
.brand span{color:var(--muted);font-weight:500;margin-left:6px}
.badge{display:inline-block;padding:3px 10px;border-radius:999px;font-size:12px;font-weight:600;border:1px solid var(--border);background:var(--panel2)}
.badge.paper{color:var(--blue);border-color:rgba(99,169,255,.4)}
.badge.live{color:var(--red);border-color:rgba(255,98,109,.5)}
.badge.research{color:var(--orange);border-color:rgba(255,182,92,.4)}
.badge.ok{color:var(--green)}.badge.danger{color:#fff;background:var(--red);border-color:var(--red)}
.badge.unknown{color:var(--muted)}
.ibkr{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:12px}
.dot{width:10px;height:10px;border-radius:50%;background:var(--muted)}
.dot.up{background:var(--green)}.dot.down{background:var(--red)}
button{font:inherit;cursor:pointer;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--text);padding:6px 12px}
button:disabled{opacity:.5;cursor:default}
button.danger{border-color:var(--red);color:var(--red)}
button.danger:hover{background:var(--red);color:#fff}
.layout{display:grid;grid-template-columns:200px 1fr;min-height:calc(100% - 52px)}
.sidebar{border-right:1px solid var(--border);padding:16px 10px;display:flex;flex-direction:column;gap:4px;background:var(--panel)}
.tab{display:block;padding:9px 12px;border-radius:8px;color:var(--muted);text-decoration:none;font-weight:500}
.tab:hover{background:var(--panel2);color:var(--text)}
.tab.active{background:var(--panel2);color:var(--text);box-shadow:inset 3px 0 0 var(--blue)}
.sidebar .legacy{margin-top:auto;font-size:12px;color:var(--muted);padding:9px 12px}
main{padding:20px 24px;max-width:1400px}
h1{font-size:18px;margin:0 0 4px}
h2{font-size:14px;margin:0 0 10px;color:var(--muted);font-weight:600;text-transform:uppercase;letter-spacing:.6px}
.sub{color:var(--muted);font-size:12px;margin-bottom:16px}
.card{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:16px;box-shadow:var(--shadow);margin-bottom:16px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:16px}
.kpi{background:var(--panel);border:1px solid var(--border);border-radius:12px;padding:14px}
.kpi-label{color:var(--muted);font-size:12px}
.kpi-value{font-size:22px;font-weight:700;margin-top:4px}
.kpi-sub{color:var(--muted);font-size:12px}
.pill{display:inline-block;padding:2px 9px;border-radius:999px;font-size:12px;font-weight:600;background:var(--panel2);border:1px solid var(--border)}
.pill.good{color:var(--green)}.pill.warn{color:var(--orange)}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap}
table{width:100%;border-collapse:collapse;font-size:13px}
th{color:var(--muted);font-weight:600;text-align:left;padding:8px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:8px;border-bottom:1px solid rgba(36,44,57,.6)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.symbol{font-weight:700}
td.empty{color:var(--muted);text-align:center;padding:18px}
.ok,.pos{color:var(--green)}.bad,.neg{color:var(--red)}.warn{color:var(--orange)}.muted{color:var(--muted)}
.blocker-text{font-size:12px;color:var(--orange);line-height:1.6;word-break:break-word}
.grid2{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px}
dl{display:grid;grid-template-columns:auto 1fr;gap:6px 14px;margin:0;font-size:13px}
dt{color:var(--muted)}dd{margin:0}
ul.list{margin:0;padding-left:18px;font-size:13px}
.summary{font-weight:600;margin-bottom:10px}
.mode-text{font-size:20px;font-weight:700}.mode-text.live{color:var(--red)}.mode-text.paper{color:var(--blue)}
.modal{position:fixed;inset:0;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;z-index:20}
.modal-box{background:var(--panel);border:1px solid var(--border);border-radius:14px;padding:20px;width:min(440px,92vw)}
.modal-box input{width:100%;padding:8px;border-radius:8px;border:1px solid var(--border);background:var(--panel2);color:var(--text);margin:10px 0}
.modal-actions{display:flex;justify-content:flex-end;gap:8px}
@media (max-width:800px){.layout{grid-template-columns:1fr}.sidebar{flex-direction:row;overflow-x:auto}.sidebar .legacy{margin:0 0 0 auto}}
</style>
</head>
<body>
<header class="topbar">
  <div class="brand">TradingMax<span>Micro-cap rebound</span></div>
  <span id="modeBadge" class="badge unknown">MODE ?</span>
  <span class="badge research">NO TRADE · RESEARCH</span>
  <span class="ibkr"><span id="ibkrDot" class="dot unknown"></span><span id="ibkrLabel">IBKR unknown</span></span>
  <span id="killState" class="badge unknown">KILL SWITCH ?</span>
  <button id="killButton" class="danger" type="button">Enable kill switch</button>
</header>
<div class="layout">
  <nav class="sidebar" aria-label="Sections">
    <a id="nav-research" class="tab active" href="#research">Research</a>
    <a id="nav-positions" class="tab" href="#positions">Positions</a>
    <a id="nav-orders" class="tab" href="#orders">Orders</a>
    <a id="nav-health" class="tab" href="#health">Health</a>
    <a class="legacy" href="/dashboard-legacy">Legacy dashboard</a>
  </nav>
  <main>
    <section id="tab-research">
      <h1>Micro-cap research</h1>
      <div class="sub row">
        <span id="microcapDecision" class="pill warn">NO TRADE</span>
        <span id="microcapStatus" class="pill warn">-</span>
        <span>Generated <span id="microcapGeneratedAt">-</span> · age <span id="microcapAge">-</span></span>
      </div>
      <div id="microcapBatchView" hidden>
        <div class="kpis">
          <div class="kpi"><div class="kpi-label">IBKR 1-minute bars</div><div id="microcapCov-ibkr_minute" class="kpi-value">-</div><div id="microcapCovPct-ibkr_minute" class="kpi-sub">-</div></div>
          <div class="kpi"><div class="kpi-label">IBKR quotes</div><div id="microcapCov-ibkr_quotes" class="kpi-value">-</div><div id="microcapCovPct-ibkr_quotes" class="kpi-sub">-</div></div>
          <div class="kpi"><div class="kpi-label">Dated roster</div><div id="microcapCov-roster_dated" class="kpi-value">-</div><div id="microcapCovPct-roster_dated" class="kpi-sub">-</div></div>
          <div class="kpi"><div class="kpi-label">News found</div><div id="microcapCov-news_found" class="kpi-value">-</div><div id="microcapCovPct-news_found" class="kpi-sub">-</div></div>
          <div class="kpi"><div class="kpi-label">SEC shares</div><div id="microcapCov-sec_shares" class="kpi-value">-</div><div id="microcapCovPct-sec_shares" class="kpi-sub">-</div></div>
        </div>
        <div class="card">
          <h2>Samples</h2>
          <div class="sub">As of <span id="microcapBatchAsOf">-</span> · <span id="microcapBatchCount">-</span> samples · bias: <span id="microcapBatchBias">-</span></div>
          <table>
            <thead><tr><th>Date</th><th>Symbol</th><th class="num">Move</th><th>IBKR 1m</th><th>Quotes</th><th>Roster</th><th class="num">News</th><th class="num">SEC</th><th>Stage errors</th></tr></thead>
            <tbody id="microcapSampleRows"></tbody>
          </table>
        </div>
      </div>
      <div id="microcapSingleView" class="card" hidden>
        <h2>Single sample</h2>
        <dl>
          <dt>Symbol</dt><dd id="microcapSampleSymbol">-</dd>
          <dt>Date</dt><dd id="microcapSampleDate">-</dd>
          <dt>Roster</dt><dd><span id="microcapRosterStatus">-</span> · <span id="microcapRosterCounts">-</span></dd>
          <dt>News</dt><dd><span id="microcapNewsStatus">-</span> · <span id="microcapNewsCount">-</span> articles</dd>
          <dt>IBKR cells</dt><dd>1m <span id="microcapIbkrMinuteCells">-</span> · quotes <span id="microcapIbkrQuoteCells">-</span></dd>
          <dt>SEC</dt><dd><span id="microcapSecStatus">-</span> · <span id="microcapSharesStatus">-</span></dd>
        </dl>
      </div>
      <div class="card">
        <h2>Blocking reasons</h2>
        <div id="microcapBlockers" class="blocker-text">-</div>
      </div>
    </section>

    <section id="tab-positions" hidden>
      <h1>Positions</h1>
      <div class="kpis">
        <div class="kpi"><div class="kpi-label">Managed</div><div id="posManagedCount" class="kpi-value">-</div></div>
        <div class="kpi"><div class="kpi-label">All broker</div><div id="posTotalCount" class="kpi-value">-</div></div>
        <div class="kpi"><div class="kpi-label">Market value</div><div id="posMarketValue" class="kpi-value">-</div></div>
        <div class="kpi"><div class="kpi-label">Unrealized P/L</div><div id="posUnrealized" class="kpi-value">-</div></div>
        <div class="kpi"><div class="kpi-label">Unprotected managed</div><div id="posUnprotected" class="kpi-value">-</div></div>
      </div>
      <div class="card">
        <h2>Managed positions</h2>
        <table>
          <thead><tr><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Avg cost</th><th class="num">Price</th><th class="num">Value</th><th class="num">Unrealized</th><th>Protection</th></tr></thead>
          <tbody id="managedRows"></tbody>
        </table>
      </div>
      <div class="card">
        <h2>All broker positions</h2>
        <table>
          <thead><tr><th>Symbol</th><th>Ownership</th><th>Side</th><th class="num">Qty</th><th class="num">Avg cost</th><th class="num">Price</th><th class="num">Value</th><th class="num">Unrealized</th></tr></thead>
          <tbody id="brokerRows"></tbody>
        </table>
      </div>
    </section>

    <section id="tab-orders" hidden>
      <h1>Open orders <span id="ordersCount" class="pill">-</span></h1>
      <div class="card">
        <table>
          <thead><tr><th>Symbol</th><th>Action</th><th class="num">Qty</th><th>Type</th><th class="num">Price</th><th>Status</th><th class="num">Order ID</th><th>Ref</th></tr></thead>
          <tbody id="orderRows"></tbody>
        </table>
      </div>
    </section>

    <section id="tab-health" hidden>
      <h1>Health &amp; safety</h1>
      <div class="grid2">
        <div class="card">
          <h2>Trading mode</h2>
          <div id="modeText" class="mode-text">-</div>
          <div id="modeAccount" class="sub">-</div>
          <div class="row">
            <button id="modePaperBtn" type="button">PAPER</button>
            <button id="modeLiveBtn" class="danger" type="button">LIVE</button>
            <span id="modeMessage" class="muted"></span>
          </div>
        </div>
        <div class="card">
          <h2>Safety</h2>
          <dl><dt>Kill switch reason</dt><dd id="safetyKillReason">-</dd></dl>
          <ul id="safetyBlockers" class="list"></ul>
        </div>
      </div>
      <div class="card">
        <h2>Runtime components</h2>
        <div id="healthSummary" class="summary">-</div>
        <table>
          <thead><tr><th>Component</th><th>Status</th><th>Criticality</th><th class="num">Age</th><th>Detail</th></tr></thead>
          <tbody id="componentRows"></tbody>
        </table>
      </div>
    </section>
  </main>
</div>

<div id="killModal" class="modal" role="dialog" aria-modal="true" hidden>
  <div class="modal-box">
    <h1 id="killModalTitle">Kill switch</h1>
    <p id="killModalText" class="muted"></p>
    <input id="killReason" type="text" maxlength="200" placeholder="Reason (required to enable)" autocomplete="off">
    <div id="killError" class="bad"></div>
    <div class="modal-actions">
      <button id="killCancel" type="button">Cancel</button>
      <button id="killConfirm" class="danger" type="button">CONFIRM</button>
    </div>
  </div>
</div>

<script src="/microcap-research-panel.js"></script>
<script src="/dashboard-app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Run all dashboard tests**

Run: `$PY -m unittest test_dashboard_layout test_microcap_readiness_route && node test_dashboard_app.js && node test_microcap_research_panel.js`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add dashboard.html test_dashboard_layout.py
git commit -m "Replace dashboard with focused micro-cap layout (sidebar tabs)

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 5: Docs and full verification

**Files:**
- Modify: `OPERATIONS.md` (the "Micro-cap Research Readiness Panel" section)

- [ ] **Step 1: Update docs**

In the readiness panel section of `OPERATIONS.md`, replace the paragraph that describes where the panel appears with:

```markdown
`/dashboard` is the focused micro-cap dashboard. The top bar holds the PAPER/LIVE badge, the NO TRADE research badge, the IBKR connection dot (grey when the broker snapshot is missing or older than 60 s) and the kill switch. Sidebar tabs: Research (schema 2 batch coverage KPIs and the sample table, or the schema 1 single-sample view), Positions, Orders, and Health (runtime components, safety blockers, PAPER/LIVE switch with double confirmation for LIVE). The selected tab is kept in the URL hash (`#research`, `#positions`, `#orders`, `#health`). The previous full dashboard remains at `/dashboard-legacy`. Scripts are served by the authenticated `/microcap-research-panel.js` and `/dashboard-app.js` routes. Removed sections keep their backend routes.
```

- [ ] **Step 2: Full verification**

Run: `$PY -m unittest test_dashboard_layout test_microcap_readiness_route test_microcap_readiness test_microcap_readiness_store && node test_dashboard_app.js && node test_microcap_research_panel.js`
Expected: all pass.

- [ ] **Step 3: Commit**

```bash
git add OPERATIONS.md
git commit -m "Document focused micro-cap dashboard

Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

---

### Task 6: Rollout to the live checkout (controller only; needs user consent before restart)

The live checkout `/home/oferke/trading-bot` has uncommitted edits in `dashboard.html`, `signal_server.py` and other bot files. Don't run `git checkout`/`reset` there.

`signal_server_core` re-reads `dashboard.html` on every request, so replacing it takes effect immediately. The new `/dashboard-app.js` route and the schema 2 store load only after a restart. **Order matters:**

- [ ] **Step 1:** Push the worktree branch to `origin/master` after a secret scan (`git diff origin/master --stat`, then grep the diff for key patterns).
- [ ] **Step 2:** Back up the live files to the session folder: `dashboard.html`, `signal_server.py`, `microcap_research_panel.js`, `microcap_readiness.py`, `microcap_readiness_store.py`.
- [ ] **Step 3:** Fast-forward the live checkout. Stash only `dashboard.html` and `signal_server.py`, run `git merge --ff-only origin/master`, then restore the two files from the backup. This brings in the new Python and JS modules and `dashboard_legacy.html`. `dashboard_legacy.html` must equal the live `dashboard.html` byte for byte (`cmp`).
- [ ] **Step 4:** Hand-apply the two new routes (Task 1, Step 4) to the live `signal_server.py`. Check with `$PY -c "import ast,sys; ast.parse(open('signal_server.py').read())"`.
- [ ] **Step 5:** Ask the user before restarting. With consent, run `sudo -n systemctl restart trading-bot.service`, then check `systemctl is-active trading-bot.service` and confirm that `/dashboard-app.js` and `/dashboard-legacy` return 401 unauthenticated and 200 authenticated (credentials read from `.env` via `dotenv_values`, never printed).
- [ ] **Step 6:** Replace the live `dashboard.html` with the new one: `git show origin/master:dashboard.html > dashboard.html`. Verify `/dashboard` returns 200 with `id="tab-research"`.
- [ ] **Step 7:** Headless smoke check through `https://trade.tradingmax.bid/dashboard` (temporary Playwright in `/tmp`, removed afterwards):
  - Research is visible.
  - Switching to `#positions`, `#orders` and `#health` shows each tab.
  - The kill modal opens and Cancel closes it without a POST.
  - No console errors.
  - Take a screenshot to the session folder.
- [ ] **Step 8:** Rollback, if needed: copy the backed-up `dashboard.html` back. It takes effect immediately with no restart.
