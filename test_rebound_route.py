import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from test_microcap_readiness_route import TestClient, isolated_server, request


class ReboundRouteTests(unittest.TestCase):
    def test_requires_auth(self):
        server, _ = isolated_server()
        status, _ = request(server.app, "/rebound-journal")
        self.assertEqual(status, 401)

    def test_returns_summary(self):
        server, _ = isolated_server()
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "trading.db")
            with sqlite3.connect(db) as conn:
                conn.execute("CREATE TABLE signals (signal_id TEXT, symbol TEXT, status TEXT, "
                             "strategy TEXT, entry_fill_price REAL, exit_fill_price REAL, "
                             "realized_pnl REAL, net_realized_pnl REAL, exit_reason TEXT, "
                             "entry_time TEXT, exit_time TEXT)")
            with patch.dict(os.environ, {"REBOUND_DB_FILE": db}):
                status, body = request(server.app, "/rebound-journal", auth=True)
        self.assertEqual(status, 200)
        self.assertEqual(body["strategy"], "microcap_rebound_v1")
        self.assertEqual(body["stats"]["trades"], 0)

    def test_failure_is_503(self):
        server, _ = isolated_server()
        with patch.object(server.rebound_journal, "summary", side_effect=sqlite3.OperationalError("x")):
            status, body = request(server.app, "/rebound-journal", auth=True)
        self.assertEqual((status, body["error"]), (503, "REBOUND_JOURNAL_UNAVAILABLE"))


if __name__ == "__main__":
    unittest.main()
