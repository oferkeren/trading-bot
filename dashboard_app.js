(function (root) {
  "use strict";

  const TABS = ["research", "positions", "orders", "health"];
  const IBKR_MAX_AGE_SECONDS = 60;
  const POLL_MS = 5000;
  const MODE_POLL_MS = 30000;
  const RESEARCH_POLL_MS = 60000;
  const REBOUND_POLL_MS = 15000;
  const REBOUND_KPI_IDS = ["reboundTrades", "reboundWinLoss", "reboundPnl"];
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

  function snapshotState(portfolio) {
    const stamp = object(portfolio).snapshot_updated_at;
    if (typeof stamp !== "string") {
      return "unavailable";
    }
    const parsed = Date.parse(stamp);
    if (!Number.isFinite(parsed)) {
      return "unavailable";
    }
    return Date.now() - parsed > IBKR_MAX_AGE_SECONDS * 1000 ? "stale" : "current";
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
    const snap = snapshotState(data);
    const managed = list(data.managed_positions);
    const legacy = list(data.legacy_positions);
    const unprotected = data.managed_unprotected_count;
    if (snap !== "current") {
      const label = snap === "stale" ? "STALE" : "UNAVAILABLE";
      POSITION_IDS.forEach(id => setText(doc, id, label, "kpi-value muted"));
    } else {
      setText(doc, "posManagedCount", String(managed.length));
      setText(doc, "posTotalCount", Number.isInteger(data.total_count)
        ? String(data.total_count) : String(managed.length + legacy.length));
      setText(doc, "posMarketValue", num(data.total_market_value));
      setText(doc, "posUnrealized", signed(data.total_position_unrealized_pnl),
        "kpi-value " + pnlClass(data.total_position_unrealized_pnl));
      setText(doc, "posUnprotected", plain(unprotected),
        "kpi-value " + (Number.isInteger(unprotected) && unprotected > 0 ? "bad" : "ok"));
    }
    fillRows(doc, "managedRows", managed.map(p => [
      [str(p.symbol), "symbol"], [str(p.side)], [plain(p.quantity), "num"],
      [num(p.avg_cost), "num"], [num(p.market_price), "num"], [num(p.market_value), "num"],
      [signed(p.unrealized_pnl), "num " + pnlClass(p.unrealized_pnl)],
      snap === "current" ? protection(p) : [snap === "stale" ? "STALE" : "UNAVAILABLE", "warn"],
    ]), snap === "current" ? "No managed positions" : snap === "stale" ? "STALE" : "UNAVAILABLE", 8);
    fillRows(doc, "brokerRows", [...managed, ...legacy].map(p => [
      [str(p.symbol), "symbol"], [str(String(p.ownership || "").toUpperCase())], [str(p.side)],
      [plain(p.quantity), "num"], [num(p.avg_cost), "num"], [num(p.market_price), "num"],
      [num(p.market_value), "num"], [signed(p.unrealized_pnl), "num " + pnlClass(p.unrealized_pnl)],
    ]), snap === "current" ? "No broker positions" : snap === "stale" ? "STALE" : "UNAVAILABLE", 8);
  }

  function renderOrders(doc, portfolio) {
    const data = object(portfolio);
    const snap = snapshotState(data);
    const orders = list(data.open_orders);
    setText(doc, "ordersCount", snap === "current" ? String(orders.length)
      : snap === "stale" ? "STALE" : "UNAVAILABLE", snap === "current" ? undefined : "muted");
    fillRows(doc, "orderRows", orders.map(o => [
      [str(o.symbol), "symbol"], [str(o.action)], [plain(o.quantity), "num"], [str(o.order_type)],
      [num(o.price), "num"], [str(o.status)], [plain(o.order_id), "num"], [str(o.order_ref)],
    ]), snap === "current" ? "No open orders" : snap === "stale" ? "STALE" : "UNAVAILABLE", 8);
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
    if (!summary) {
      fillList(doc, "safetyBlockers", ["UNAVAILABLE"], "UNAVAILABLE");
      setText(doc, "safetyKillReason", "UNAVAILABLE");
    } else {
      const data = object(summary);
      const blockers = Array.isArray(object(data.safety).blockers)
        ? data.safety.blockers.filter(item => typeof item === "string" && item.length) : [];
      fillList(doc, "safetyBlockers", blockers, "No safety blockers");
      setText(doc, "safetyKillReason", str(object(data.system).kill_switch_reason));
    }
  }

  function renderRebound(doc, data) {
    const body = object(data);
    const stats = object(body.stats);
    setText(doc, "reboundTrades", plain(stats.trades));
    setText(doc, "reboundWinLoss", `${plain(stats.wins)} / ${plain(stats.losses)}`);
    setText(doc, "reboundPnl", signed(stats.total_pnl), pnlClass(stats.total_pnl));
    const order = ["EARLY_PRE", "PRE", "RTH", "POST", "UNKNOWN"];
    const sessions = object(body.stats_by_session);
    const parts = order.filter(name => sessions[name]).map(name => {
      const s = object(sessions[name]);
      return `${name}: ${plain(s.trades)} trades, ${plain(s.wins)}W/${plain(s.losses)}L, ${signed(s.total_pnl)}`;
    });
    setText(doc, "reboundSessions", parts.length ? parts.join(" · ") : "No closed trades by session yet");
    fillRows(doc, "reboundOpenRows", list(body.open_positions).map(item => [
      [str(item.symbol)], [num(item.entry, 4), "num"], [num(item.initial_stop, 4), "num"],
      [num(item.current_stop, 4), "num"], [num(item.high_since_entry, 4), "num"],
      [str(item.opened_at)], [str(item.state)],
    ]), "No open rebound position", 7);
    fillRows(doc, "reboundTradeRows", list(body.trades).map(item => [
      [str(item.exit_time)], [str(item.symbol)], [str(item.status)],
      [num(item.entry_fill_price, 4), "num"], [num(item.exit_fill_price, 4), "num"],
      [signed(item.pnl), `num ${pnlClass(item.pnl)}`.trim()],
    ]), "No closed rebound trades yet", 6);
    fillRows(doc, "reboundEventRows", list(body.events).slice(0, 20).map(item => [
      [str(item.ts)], [str(item.event)], [str(item.symbol)], [str(item.reason)],
    ]), "No decisions yet", 4);
  }

  async function fetchJson(fetchImpl, url, options, timerSource) {
    const timers = timerSource || root;
    const controller = typeof (timers || {}).AbortController === "function"
      ? new timers.AbortController()
      : typeof AbortController === "function" ? new AbortController() : null;
    const request = Object.assign({ credentials: "same-origin", cache: "no-store" }, options || {});
    let timeoutId = null;
    if (controller) {
      request.signal = controller.signal;
      timeoutId = timers.setTimeout(() => { controller.abort(); }, 8000);
    }
    try {
      const reply = await fetchImpl(url, request);
      if (!reply || reply.ok !== true) {
        throw new Error("HTTP_ERROR");
      }
      return await reply.json();
    } finally {
      if (timeoutId !== null) {
        timers.clearTimeout(timeoutId);
      }
    }
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
        state.mode = await fetchJson(fetchImpl, "/trading-mode", undefined, win);
      } catch (error) {
        state.mode = null;
      }
      renderModePanel(doc, state.mode);
    }

    async function refreshTop() {
      const [summary, status] = await Promise.allSettled([
        fetchJson(fetchImpl, "/dashboard-summary", undefined, win),
        fetchJson(fetchImpl, "/status", undefined, win)]);
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
          const portfolio = await fetchJson(fetchImpl, "/dashboard-portfolio", undefined, win);
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
          renderHealth(doc, await fetchJson(fetchImpl, "/dashboard-components", undefined, win),
            state.summary);
        } catch (error) {
          markUnavailable(doc, ["healthSummary"]);
          fillRows(doc, "componentRows", [], "UNAVAILABLE", 5);
          fillList(doc, "safetyBlockers", ["UNAVAILABLE"], "UNAVAILABLE");
          setText(doc, "safetyKillReason", "UNAVAILABLE");
        }
        renderModePanel(doc, state.mode);
      }
    }

    async function refreshResearch() {
      if (panel && typeof panel.loadMicrocapResearch === "function") {
        await panel.loadMicrocapResearch(
          (url, options) => fetchJson(fetchImpl, url, options, win)
            .then(body => ({ ok: true, json: async () => body })),
          id => doc.getElementById(id), tag => doc.createElement(tag));
      }
    }

    async function refreshRebound() {
      try {
        renderRebound(doc, await fetchJson(fetchImpl, "/rebound-journal", {}, win));
      } catch (error) {
        markUnavailable(doc, REBOUND_KPI_IDS);
        setText(doc, "reboundSessions", "UNAVAILABLE");
        fillRows(doc, "reboundOpenRows", [], "UNAVAILABLE", 7);
        fillRows(doc, "reboundTradeRows", [], "UNAVAILABLE", 6);
        fillRows(doc, "reboundEventRows", [], "UNAVAILABLE", 4);
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
        }, win);
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
        }, win);
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
      const pollRebound = guarded("rebound", refreshRebound);
      const pollMode = guarded("mode", loadMode);
      win.addEventListener("hashchange", () => {
        selectTab(win.location.hash);
        pollActive();
      });
      selectTab(win.location.hash);
      pollMode().then(pollTop);
      pollActive();
      pollResearch();
      pollRebound();
      win.setInterval(() => { pollTop(); pollActive(); }, POLL_MS);
      win.setInterval(pollMode, MODE_POLL_MS);
      win.setInterval(pollResearch, RESEARCH_POLL_MS);
      win.setInterval(pollRebound, REBOUND_POLL_MS);
    }

    return { start, selectTab, refreshTop, refreshActive, refreshResearch, refreshRebound, openKill,
      closeKill, confirmKill, changeMode, state };
  }

  const api = { TABS, tabFromHash, ibkrState, modeLabel, killSwitchBody, modeConfirmations,
    renderTopBar, renderPositions, renderOrders, renderHealth, renderRebound, markUnavailable,
    createApp };
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
