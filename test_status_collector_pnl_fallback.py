import math
import unittest
from unittest.mock import patch

import status_collector


class AccountUpdatePnlFallbackTests(unittest.TestCase):
    def test_derives_daily_pnl_from_base_account_update_ledger_values(self):
        app = status_collector.StatusApp()
        warnings = []

        with patch.object(status_collector, "IB_ACCOUNT", "DUQ569670"):
            app.updateAccountValue(
                "$LEDGER-RealizedPnL",
                "999.00",
                "USD",
                "DUQ569670",
            )
            app.updateAccountValue(
                "$LEDGER-UnrealizedPnL",
                "999.00",
                "USD",
                "DUQ569670",
            )
            app.updateAccountValue(
                "$LEDGER-RealizedPnL",
                "1.25",
                "BASE",
                "DUQ569670",
            )
            app.updateAccountValue(
                "$LEDGER-UnrealizedPnL",
                "2.75",
                "BASE",
                "DUQ569670",
            )

            derived = status_collector.apply_account_update_pnl_fallback(
                app,
                warnings,
            )

        self.assertTrue(derived)
        self.assertEqual(app.realized_pnl, 1.25)
        self.assertEqual(app.unrealized_pnl, 2.75)
        self.assertEqual(app.daily_pnl, 4.0)
        self.assertIn(
            "Account daily P/L derived from account updates",
            warnings,
        )

    def test_leaves_daily_pnl_none_when_account_update_values_missing_or_non_finite(self):
        app = status_collector.StatusApp()
        warnings = []

        with patch.object(status_collector, "IB_ACCOUNT", "DUQ569670"):
            app.updateAccountValue(
                "$LEDGER-RealizedPnL",
                "nan",
                "BASE",
                "DUQ569670",
            )

            derived = status_collector.apply_account_update_pnl_fallback(
                app,
                warnings,
            )

        self.assertFalse(derived)
        self.assertIsNone(app.daily_pnl)
        self.assertTrue(
            app.realized_pnl is None
            or math.isnan(app.realized_pnl)
        )
        self.assertEqual(warnings, [])

    def test_leaves_daily_pnl_none_when_positions_exist_despite_account_update_values(self):
        app = status_collector.StatusApp()
        app.positions.append({
            "symbol": "SORA",
            "quantity": 10.0,
        })
        warnings = []

        with patch.object(status_collector, "IB_ACCOUNT", "DUQ569670"):
            app.updateAccountValue(
                "$LEDGER-RealizedPnL",
                "-10.00",
                "BASE",
                "DUQ569670",
            )
            app.updateAccountValue(
                "$LEDGER-UnrealizedPnL",
                "100.00",
                "BASE",
                "DUQ569670",
            )

            derived = status_collector.apply_account_update_pnl_fallback(
                app,
                warnings,
            )

        self.assertFalse(derived)
        self.assertIsNone(app.daily_pnl)
        self.assertNotIn(
            "Account daily P/L derived from account updates",
            warnings,
        )
        self.assertEqual(warnings, [])


if __name__ == "__main__":
    unittest.main()
