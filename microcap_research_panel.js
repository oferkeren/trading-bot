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

  function renderMicrocapResearch(data, getElement, createElement) {
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
    set(getElement, "microcapBlockers", blockers || "-", "blocker-text");
    const batchMode = snapshot.schema_version === 2;
    const hasSample = Object.keys(sample).length > 0;
    show(getElement, "microcapBatchView", batchMode);
    show(getElement, "microcapSingleView", !batchMode && hasSample);
    if (batchMode) {
      renderBatch(snapshot, getElement, createElement || defaultCreateElement());
    }
  }

  async function loadMicrocapResearch(fetchImpl, getElement, createElement) {
    try {
      const response = await fetchImpl("/microcap-research-status");
      if (!response || response.ok !== true) {
        throw new Error("micro-cap status unavailable");
      }
      renderMicrocapResearch(await response.json(), getElement, createElement);
    } catch (error) {
      renderMicrocapResearch({
        status: "UNAVAILABLE",
        decision: "NO_TRADE",
        order_approval: false,
        blockers: ["READINESS_UNAVAILABLE"],
      }, getElement, createElement);
    }
  }

  const api = { renderMicrocapResearch, loadMicrocapResearch };
  if (typeof module !== "undefined" && module.exports) {
    module.exports = api;
  }
  root.renderMicrocapResearch = renderMicrocapResearch;
  root.loadMicrocapResearch = loadMicrocapResearch;
})(typeof window !== "undefined" ? window : globalThis);
