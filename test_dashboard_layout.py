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
