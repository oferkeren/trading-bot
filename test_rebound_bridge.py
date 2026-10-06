import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import ai_signal_bridge
import rebound_journal
import signal_bridge

CANDIDATE = {"symbol": "ABC", "action": "BUY", "entry": 2.0, "stop": 1.9, "target": 2.3,
             "qualified": True, "strategy": "microcap_rebound_v1", "timeframe": "1m"}
STRONG = {"status": "PASS", "news_count": 2, "ai": {"news_score": 0.8, "event_type": "CONTRACT"}}


class ProcessReboundCandidateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = os.path.join(self.tmp.name, "trading.db")
        with sqlite3.connect(self.db) as conn:
            conn.execute("CREATE TABLE signals (signal_id TEXT, strategy TEXT, status TEXT)")
        self.original = patch.object(ai_signal_bridge, "_original_process_candidate",
                                     return_value={"signal_id": "sig-1", "status": "SENT"})
        self.post = self.original.start()
        self.paper = patch("rebound_strategy.paper_guard", return_value=True)
        self.paper.start()

    def tearDown(self):
        patch.stopall()
        self.tmp.cleanup()

    def run_with(self, gate):
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate", gate):
            return ai_signal_bridge.process_rebound_candidate(dict(CANDIDATE), "s", db_file=self.db)

    def journal(self):
        with sqlite3.connect(self.db) as conn:
            return conn.execute("SELECT event, reason FROM rebound_journal ORDER BY id").fetchall()

    def journal_with_detail(self):
        with sqlite3.connect(self.db) as conn:
            rows = conn.execute(
                "SELECT event, reason, detail FROM rebound_journal ORDER BY id"
            ).fetchall()
        return [(event, reason, json.loads(detail) if detail else None)
                for event, reason, detail in rows]

    def test_strong_news_posts_and_journals_signal(self):
        original = dict(CANDIDATE)
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate",
                          MagicMock(return_value=STRONG)):
            outcome = ai_signal_bridge.process_rebound_candidate(original, "s", db_file=self.db)
        self.assertEqual(outcome["signal_id"], "sig-1")
        self.post.assert_called_once()
        posted_candidate = self.post.call_args.args[0]
        self.assertIsNot(posted_candidate, original)
        self.assertNotIn("_ai_final_quantity", original)
        self.assertEqual(posted_candidate["_ai_final_quantity"], 500)
        self.assertEqual(self.journal_with_detail()[0][2]["quantity"], 500)

    def test_rebound_payload_includes_designed_quantity_without_gate_marker(self):
        self.run_with(MagicMock(return_value=STRONG))
        posted_candidate = self.post.call_args.args[0]
        self.assertTrue(posted_candidate["_rebound_gated"])
        payload = ai_signal_bridge.build_payload(posted_candidate, "secret")
        self.assertEqual(payload["quantity"], 500)
        self.assertEqual(payload["strategy"], "microcap_rebound_v1")
        self.assertNotIn("_rebound_gated", payload)

    def test_large_entry_skips_when_designed_size_is_below_one_share(self):
        candidate = {**CANDIDATE, "entry": 1500.0}
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate",
                          MagicMock(return_value=STRONG)):
            outcome = ai_signal_bridge.process_rebound_candidate(candidate, "s", db_file=self.db)
        self.assertEqual(outcome["reason"], "SKIP_SIZE")
        self.post.assert_not_called()
        self.assertEqual(self.journal(), [("SKIP", "SKIP_SIZE")])

    def test_wide_stop_sizes_down_by_risk(self):
        candidate = {**CANDIDATE, "entry": 2.0, "stop": 1.88}
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate",
                          MagicMock(return_value=STRONG)):
            ai_signal_bridge.process_rebound_candidate(candidate, "s", db_file=self.db)
        posted_candidate = self.post.call_args.args[0]
        self.assertEqual(posted_candidate["_ai_final_quantity"], 458)
        self.assertLessEqual(458 * 0.12, 55.0)

    def test_stop_not_below_entry_skips_size(self):
        candidate = {**CANDIDATE, "entry": 2.0, "stop": 2.0}
        with patch.object(ai_signal_bridge.ai_gate, "evaluate_trade_candidate",
                          MagicMock(return_value=STRONG)):
            outcome = ai_signal_bridge.process_rebound_candidate(candidate, "s", db_file=self.db)
        self.assertEqual(outcome["reason"], "SKIP_SIZE")
        self.post.assert_not_called()

    def test_weak_news_is_skipped(self):
        weak = {**STRONG, "ai": {"news_score": 0.2, "event_type": "CONTRACT"}}
        outcome = self.run_with(MagicMock(return_value=weak))
        self.assertEqual(outcome["reason"], "NEWS_NOT_STRONG")
        self.post.assert_not_called()
        self.assertEqual(self.journal(), [("SKIP", "NEWS_NOT_STRONG")])

    def test_gate_error_fails_closed(self):
        outcome = self.run_with(MagicMock(side_effect=TimeoutError("news timeout")))
        self.assertEqual(outcome["reason"], "NEWS_GATE_ERROR")
        self.post.assert_not_called()

    def test_busy_skips_before_gate(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute("INSERT INTO signals VALUES ('x', 'microcap_rebound_v1', 'SUBMITTED')")
        gate = MagicMock(return_value=STRONG)
        outcome = self.run_with(gate)
        self.assertEqual(outcome["reason"], "SKIP_BUSY")
        gate.assert_not_called()

    def test_not_paper_skips(self):
        with patch("rebound_strategy.paper_guard", return_value=False):
            outcome = self.run_with(MagicMock(return_value=STRONG))
        self.assertEqual(outcome["reason"], "SKIP_NOT_PAPER")
        self.post.assert_not_called()

    def test_process_candidate_routes_rebound(self):
        with patch.object(ai_signal_bridge, "process_rebound_candidate",
                          return_value={"status": "X"}) as strict:
            self.assertEqual(ai_signal_bridge.process_candidate(dict(CANDIDATE), "s"), {"status": "X"})
        strict.assert_called_once()


class OriginalBridgeReboundGateTests(unittest.TestCase):
    def setUp(self):
        self.settings = {
            "BRIDGE_MODE": signal_bridge.BRIDGE_MODE,
            "BRIDGE_DRY_RUN": signal_bridge.BRIDGE_DRY_RUN,
            "BRIDGE_ALLOW_POST": signal_bridge.BRIDGE_ALLOW_POST,
        }
        signal_bridge.BRIDGE_MODE = "TEST"
        signal_bridge.BRIDGE_DRY_RUN = False
        signal_bridge.BRIDGE_ALLOW_POST = True

    def tearDown(self):
        for key, value in self.settings.items():
            setattr(signal_bridge, key, value)

    def test_original_process_candidate_rejects_ungated_rebound_without_posting(self):
        with patch.object(signal_bridge, "send_payload") as send:
            outcome = signal_bridge.process_candidate(dict(CANDIDATE), "secret")
        self.assertEqual(outcome, {"status": "REBOUND_UNGATED", "symbol": "ABC",
                                   "reason": "REBOUND_REQUIRES_AI_BRIDGE"})
        send.assert_not_called()

    def test_original_process_candidate_posts_gated_rebound(self):
        candidate = dict(CANDIDATE, _rebound_gated=True)
        with patch.object(signal_bridge, "signal_already_sent", return_value=False), \
                patch.object(signal_bridge, "symbol_action_in_cooldown", return_value=(False, None)), \
                patch.object(signal_bridge, "enforce_mode_api", return_value={"mode": "TEST", "test_mode": True}), \
                patch.object(signal_bridge, "record_sent_signal"), \
                patch.object(signal_bridge, "send_payload", return_value=(200, {"ok": True}, "ok")) as send:
            outcome = signal_bridge.process_candidate(candidate, "secret")
        self.assertEqual(outcome["status"], "TEST_SENT")
        payload = send.call_args.args[0]
        self.assertEqual(payload["strategy"], "microcap_rebound_v1")
        self.assertNotIn("_rebound_gated", payload)


class ReboundModeTests(unittest.TestCase):
    def test_collect_dispatches_rebound(self):
        with tempfile.TemporaryDirectory() as home:
            mode_dir = Path(home) / ".cache" / "tradingmax"
            mode_dir.mkdir(parents=True)
            (mode_dir / "strategy_mode.txt").write_text("REBOUND\n", encoding="utf-8")
            app = MagicMock()
            app.market_data = {1: {"bid": 1.99, "ask": 2.0}}
            app.isConnected.return_value = False
            skip = {"symbol": "ABC", "qualified": False, "skip_reason": "SKIP_CYCLES",
                    "rebound": {"cycles": 1}, "strategy": "microcap_rebound_v1"}
            with patch.object(signal_bridge.Path, "home", return_value=Path(home)), \
                    patch.object(signal_bridge, "mark_phase"), \
                    patch.object(signal_bridge, "record_bridge_mode"), \
                    patch.object(signal_bridge, "record_analysis"), \
                    patch.object(signal_bridge.strategy_engine, "get_candidates",
                                 return_value=[{"symbol": "ABC"}]), \
                    patch.object(signal_bridge.strategy_engine, "TradingMaxStrategy", return_value=app), \
                    patch.object(signal_bridge.strategy_engine, "connect_strategy"), \
                    patch.object(signal_bridge.strategy_engine, "start_live", return_value={"ABC": 1}), \
                    patch.object(signal_bridge.strategy_engine, "stop_live"), \
                    patch.object(signal_bridge.strategy_engine, "request_history") as momentum, \
                    patch.object(signal_bridge.scalp_strategy, "request_history") as scalp, \
                    patch.object(signal_bridge.rebound_strategy, "request_history", return_value=[]), \
                    patch.object(signal_bridge.rebound_strategy, "analyze", return_value=skip) as analyze, \
                    patch.object(signal_bridge.rebound_journal, "record_skip") as record_skip, \
                    patch.object(signal_bridge.time, "sleep"):
                results = signal_bridge.collect_strategy_results()
        self.assertEqual(results, [skip])
        self.assertEqual(analyze.call_args.args[2], {"bid": 1.99, "ask": 2.0})
        record_skip.assert_called_once_with(rebound_journal.DEFAULT_DB, "ABC", "SKIP_CYCLES", {"cycles": 1})
        momentum.assert_not_called()
        scalp.assert_not_called()

    def test_server_accepts_rebound_mode(self):
        source = Path(signal_bridge.__file__).with_name("signal_server_core.py").read_text(encoding="utf-8")
        start = source.index("VALID_STRATEGY_MODES = {")
        self.assertIn('"REBOUND"', source[start:source.index("}", start)])
        listing = source.index('"valid_modes":')
        self.assertIn('"REBOUND"', source[listing:source.index("]", listing)])


if __name__ == "__main__":
    unittest.main()
