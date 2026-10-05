const assert = require("node:assert/strict");
const { renderMicrocapResearch, loadMicrocapResearch } = require("./microcap_research_panel.js");

function elementMap(ids) {
  const nodes = Object.fromEntries(ids.map(id => [id, { textContent: "", className: "initial" }]));
  return { nodes, getElement: id => nodes[id] || null };
}

const ids = [
  "microcapDecision", "microcapStatus", "microcapSampleSymbol", "microcapSampleDate",
  "microcapGeneratedAt", "microcapAge", "microcapRosterStatus", "microcapRosterCounts",
  "microcapNewsStatus", "microcapNewsCount", "microcapIbkrMinuteCells",
  "microcapIbkrQuoteCells", "microcapSecStatus", "microcapSharesStatus",
  "microcapBlockers",
];

async function testValidSnapshot() {
  const { nodes, getElement } = elementMap(ids);
  renderMicrocapResearch({
    status: "CURRENT",
    generated_at: "2026-10-05T11:00:00Z",
    age_seconds: 42,
    decision: "NO_TRADE",
    order_approval: false,
    sample: { issuer_id: "issuer-1", symbol: "SORA", date: "2025-05-28" },
    sample_window: { start_utc: "2025-05-28T13:30:00Z", end_utc: "2025-05-28T20:00:00Z" },
    sources: {
      roster: { status: "UNVERIFIED", evidence_status: "DATED_ROSTER_OBSERVED", active_count: 5, inactive_count: 2 },
      news: { status: "ARTICLES_OBSERVED", article_count: 3 },
      ibkr: { minute_cells: 1, quote_cells: 1 },
      shares: { status: "MARKET_CAP_UNVERIFIED", sec_observation_count: 1 },
      sec: { status: "OBSERVED", observation_count: 1, verification: "UNVERIFIED" },
    },
    blockers: ["MARKET_CAP_UNVERIFIED", "ROSTER_COVERAGE_UNVERIFIED"],
  }, getElement);

  assert.equal(nodes.microcapDecision.textContent, "NO TRADE");
  assert.equal(nodes.microcapStatus.textContent, "CURRENT");
  assert.match(nodes.microcapStatus.className, /current/);
  assert.equal(nodes.microcapSampleSymbol.textContent, "SORA");
  assert.equal(nodes.microcapSampleDate.textContent, "2025-05-28");
  assert.equal(nodes.microcapGeneratedAt.textContent, "2026-10-05T11:00:00Z");
  assert.equal(nodes.microcapAge.textContent, "42s");
  assert.equal(nodes.microcapRosterStatus.textContent, "UNVERIFIED / DATED_ROSTER_OBSERVED");
  assert.equal(nodes.microcapRosterCounts.textContent, "active 5 / inactive 2");
  assert.equal(nodes.microcapNewsStatus.textContent, "ARTICLES_OBSERVED");
  assert.equal(nodes.microcapNewsCount.textContent, "3");
  assert.equal(nodes.microcapIbkrMinuteCells.textContent, "1");
  assert.equal(nodes.microcapIbkrQuoteCells.textContent, "1");
  assert.equal(nodes.microcapSecStatus.textContent, "OBSERVED");
  assert.equal(nodes.microcapSharesStatus.textContent, "MARKET_CAP_UNVERIFIED");
  assert.equal(nodes.microcapBlockers.textContent, "MARKET_CAP_UNVERIFIED, ROSTER_COVERAGE_UNVERIFIED");
}

async function testWarnStates() {
  for (const status of ["STALE", "UNAVAILABLE"]) {
    const { nodes, getElement } = elementMap(ids);
    renderMicrocapResearch({ status, decision: "NO_TRADE", blockers: ["READINESS_" + status] }, getElement);
    assert.equal(nodes.microcapStatus.textContent, status);
    assert.match(nodes.microcapStatus.className, /warn/);
    assert.equal(nodes.microcapDecision.textContent, "NO TRADE");
  }
}

async function testForgedApprovalIsNeverShown() {
  const { nodes, getElement } = elementMap(ids);
  renderMicrocapResearch({ decision: "TRADE", order_approval: true, status: "CURRENT" }, getElement);
  assert.equal(nodes.microcapDecision.textContent, "NO TRADE");
}

async function testMaliciousStringsUseTextOnly() {
  const { nodes, getElement } = elementMap(ids);
  const attack = "<img src=x onerror=alert(1)>";
  renderMicrocapResearch({
    status: attack,
    generated_at: attack,
    sample: { symbol: attack, date: attack },
    sources: {
      roster: { status: attack, evidence_status: attack, active_count: attack, inactive_count: attack },
      news: { status: attack, article_count: attack },
      ibkr: { minute_cells: attack, quote_cells: attack },
      shares: { status: attack },
      sec: { status: attack },
    },
    blockers: [attack],
  }, getElement);
  assert.equal(nodes.microcapSampleSymbol.textContent, attack);
  assert.equal(nodes.microcapBlockers.textContent, attack);
  for (const node of Object.values(nodes)) {
    assert.equal(Object.prototype.hasOwnProperty.call(node, "innerHTML"), false);
  }
}

async function testMissingGarbageFieldsRenderDash() {
  const { nodes, getElement } = elementMap(ids);
  renderMicrocapResearch({ status: 17, sample: null, sources: { roster: [], news: "bad" }, blockers: "bad" }, getElement);
  assert.equal(nodes.microcapStatus.textContent, "-");
  assert.equal(nodes.microcapSampleSymbol.textContent, "-");
  assert.equal(nodes.microcapRosterCounts.textContent, "active - / inactive -");
  assert.equal(nodes.microcapBlockers.textContent, "-");
}

async function testLoadSuccessAndFailures() {
  const success = elementMap(ids);
  await loadMicrocapResearch(async url => ({ ok: true, json: async () => ({ status: "CURRENT", sample: { symbol: "SORA" } }) }), success.getElement);
  assert.equal(success.nodes.microcapStatus.textContent, "CURRENT");
  assert.equal(success.nodes.microcapSampleSymbol.textContent, "SORA");

  for (const fetchImpl of [
    async () => { throw new Error("network"); },
    async () => ({ ok: false, status: 500, json: async () => ({ status: "CURRENT" }) }),
    async () => ({ ok: true, json: async () => { throw new Error("bad json"); } }),
  ]) {
    const failed = elementMap(ids);
    await loadMicrocapResearch(fetchImpl, failed.getElement);
    assert.equal(failed.nodes.microcapDecision.textContent, "NO TRADE");
    assert.equal(failed.nodes.microcapStatus.textContent, "UNAVAILABLE");
    assert.match(failed.nodes.microcapStatus.className, /warn/);
  }
}

(async () => {
  await testValidSnapshot();
  await testWarnStates();
  await testForgedApprovalIsNeverShown();
  await testMaliciousStringsUseTextOnly();
  await testMissingGarbageFieldsRenderDash();
  await testLoadSuccessAndFailures();
  console.log("microcap_research_panel tests passed");
})().catch(error => {
  console.error(error);
  process.exit(1);
});
