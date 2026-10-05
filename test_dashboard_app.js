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
