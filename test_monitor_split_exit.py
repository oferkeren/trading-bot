import unittest

import trade_monitor_core as tmc


def fill(shares, price, time="t"):
    return {"shares": float(shares), "price": float(price), "time": time}


class MergeSplitExitTests(unittest.TestCase):
    def test_oca_split_that_matches_entry_closes_as_dominant_leg(self):
        leg, merged = tmc.merge_split_exit(
            fill(176, 2.84), fill(1, 3.10, "a"), fill(175, 3.10, "b"))
        self.assertEqual(leg, "SL")
        self.assertEqual(merged["shares"], 176.0)
        self.assertAlmostEqual(merged["price"], 3.10)
        self.assertEqual(merged["time"], "b")

    def test_weighted_price_and_tp_dominant(self):
        leg, merged = tmc.merge_split_exit(fill(100, 2.0), fill(60, 3.0), fill(40, 1.5))
        self.assertEqual(leg, "TP")
        self.assertAlmostEqual(merged["price"], 2.4)

    def test_partial_split_stays_mergeable(self):
        leg, merged = tmc.merge_split_exit(fill(100, 2.0), fill(10, 3.0), fill(20, 1.9))
        self.assertEqual(leg, "SL")
        self.assertEqual(merged["shares"], 30.0)

    def test_overfill_is_a_real_double_exit(self):
        self.assertIsNone(tmc.merge_split_exit(fill(100, 2.0), fill(100, 3.0), fill(1, 1.9)))

    def test_missing_entry_is_a_real_double_exit(self):
        self.assertIsNone(tmc.merge_split_exit(None, fill(1, 3.0), fill(1, 1.9)))


if __name__ == "__main__":
    unittest.main()
