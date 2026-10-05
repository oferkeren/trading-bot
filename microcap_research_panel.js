(function (root) {
  "use strict";

  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }

  function text(value) {
    return typeof value === "string" && value.length ? value : "-";
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? String(value) : "-";
  }

  function age(value) {
    if (!Number.isFinite(value) || value < 0) {
      return "-";
    }
    const seconds = Math.floor(value);
    if (seconds < 60) {
      return seconds + "s";
    }
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) {
      return minutes + "m";
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 48) {
      return hours + "h";
    }
    return Math.floor(hours / 24) + "d";
  }

  function set(getElement, id, value, className) {
    const element = getElement(id);
    if (!element) {
      return;
    }
    element.textContent = value;
    if (className !== undefined) {
      element.className = className;
    }
  }

  function statusClass(status) {
    if (status === "CURRENT") {
      return "pill good current";
    }
    if (status === "STALE" || status === "UNAVAILABLE") {
      return "pill warn";
    }
    return "pill warn";
  }

  function renderMicrocapResearch(data, getElement) {
    const snapshot = object(data);
    const status = text(snapshot.status);
    const sample = object(snapshot.sample);
    const sources = object(snapshot.sources);
    const roster = object(sources.roster);
    const news = object(sources.news);
    const ibkr = object(sources.ibkr);
    const sec = object(sources.sec);
    const shares = object(sources.shares);
    const blockers = Array.isArray(snapshot.blockers)
      ? snapshot.blockers.filter(item => typeof item === "string" && item.length).join(", ")
      : "-";

    set(getElement, "microcapDecision", "NO TRADE", "pill warn");
    set(getElement, "microcapStatus", status, statusClass(status));
    set(getElement, "microcapSampleSymbol", text(sample.symbol));
    set(getElement, "microcapSampleDate", text(sample.date));
    set(getElement, "microcapGeneratedAt", text(snapshot.generated_at));
    set(getElement, "microcapAge", age(snapshot.age_seconds));

    const rosterStatus = [text(roster.status), text(roster.evidence_status)]
      .filter(part => part !== "-")
      .join(" / ") || "-";
    set(getElement, "microcapRosterStatus", rosterStatus);
    set(
      getElement,
      "microcapRosterCounts",
      "active " + count(roster.active_count) + " / inactive " + count(roster.inactive_count)
    );

    set(getElement, "microcapNewsStatus", text(news.status));
    set(getElement, "microcapNewsCount", count(news.article_count));
    set(getElement, "microcapIbkrMinuteCells", count(ibkr.minute_cells));
    set(getElement, "microcapIbkrQuoteCells", count(ibkr.quote_cells));
    set(getElement, "microcapSecStatus", text(sec.status));
    set(getElement, "microcapSharesStatus", text(shares.status));
    set(getElement, "microcapBlockers", blockers || "-");
  }

  async function loadMicrocapResearch(fetchImpl, getElement) {
    try {
      const response = await fetchImpl("/microcap-research-status");
      if (!response || response.ok !== true) {
        throw new Error("micro-cap status unavailable");
      }
      renderMicrocapResearch(await response.json(), getElement);
    } catch (error) {
      renderMicrocapResearch({
        status: "UNAVAILABLE",
        decision: "NO_TRADE",
        order_approval: false,
        blockers: ["READINESS_UNAVAILABLE"],
      }, getElement);
    }
  }

  const api = { renderMicrocapResearch, loadMicrocapResearch };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.renderMicrocapResearch = renderMicrocapResearch;
  root.loadMicrocapResearch = loadMicrocapResearch;
})(typeof window !== "undefined" ? window : globalThis);
