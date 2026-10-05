import unittest
from decimal import Decimal

from microcap_cap_evidence import classify_microcap


DECISION_AT = "2025-05-28T15:00:00Z"


def evidence(*, raw_close: object = 3.0, count: object = 100_000_000,
             price_overrides: dict[str, object] | None = None,
             share_overrides: dict[str, object] | None = None,
             shares: list[dict[str, object]] | None = None,
             source_verified: bool = True,
             **arguments: object) -> dict[str, object]:
    price: dict[str, object] = {
        "raw_close": raw_close,
        "known_at": "2025-05-28T14:59:00Z",
        "basis": "raw",
        "issuer_id": "issuer-1",
        "class_id": "common",
        "class_type": "common",
    }
    if price_overrides:
        price.update(price_overrides)
    share: dict[str, object] = {
        "count": count,
        "issuer_id": "issuer-1",
        "class_id": "common",
        "class_type": "common",
        "filed_at": "2025-05-28T14:00:00Z",
        "available_at": "2025-05-28T14:01:00Z",
        "basis": "raw",
        "source": "audited-filing",
    }
    if share_overrides:
        share.update(share_overrides)
    return classify_microcap(
        issuer_id="issuer-1",
        decision_at=DECISION_AT,
        price=price,
        shares=shares if shares is not None else [share],
        classes_complete=True,
        corporate_actions_verified=True,
        source_verified=source_verified,
        **arguments,
    )


class CapitalizationEvidenceTests(unittest.TestCase):
    def test_exact_three_hundred_million_cap_is_inclusive(self) -> None:
        result = evidence(raw_close=3.0, count=100_000_000)

        self.assertEqual(result["status"], "MICROCAP")
        self.assertEqual(result["raw_market_cap"], 300_000_000)
        self.assertEqual(result["raw_close"], 3.0)
        self.assertEqual(result["shares_count"], 100_000_000)
        self.assertEqual(result["decision_at"], DECISION_AT)
        self.assertEqual(result["price_known_at"], "2025-05-28T14:59:00Z")
        self.assertEqual(result["shares_filed_at"], "2025-05-28T14:00:00Z")
        self.assertEqual(result["shares_available_at"], "2025-05-28T14:01:00Z")

    def test_valid_source_identifiers_are_reported_without_copying_evidence(self) -> None:
        result = evidence(
            price_overrides={
                "source": "ibkr-bars",
                "api_token": "never-output",
                "provider_payload": {"secret": "never-output"},
            },
            share_overrides={
                "source": "sec-companyfacts",
                "api_token": "never-output",
                "provider_payload": {"secret": "never-output"},
            },
        )

        self.assertEqual(result["price_source"], "ibkr-bars")
        self.assertEqual(result["shares_source"], "sec-companyfacts")
        self.assertNotIn("api_token", result)
        self.assertNotIn("provider_payload", result)
        self.assertNotIn("never-output", repr(result))

    def test_source_identifiers_are_validated_for_their_evidence_role(self) -> None:
        result = evidence(
            price_overrides={"source": "sec-companyfacts"},
            share_overrides={"source": "ibkr-bars"},
        )

        self.assertEqual(result["status"], "MICROCAP")
        self.assertNotIn("price_source", result)
        self.assertNotIn("shares_source", result)

    def test_audited_filing_is_a_valid_shares_source(self) -> None:
        result = evidence(share_overrides={"source": "audited-filing"})

        self.assertEqual(result["shares_source"], "audited-filing")

    def test_arbitrary_source_text_is_not_copied_to_result(self) -> None:
        result = evidence(
            price_overrides={"source": "api_token=never-output"},
            share_overrides={"source": "SEC filing\napi_token=never-output"},
        )

        self.assertNotIn("price_source", result)
        self.assertNotIn("shares_source", result)
        self.assertNotIn("never-output", repr(result))

    def test_unrecognized_token_shaped_sources_are_not_copied(self) -> None:
        token = "credential-shaped-placeholder"
        result = evidence(
            price_overrides={"source": token},
            share_overrides={"source": token},
        )

        self.assertNotIn("price_source", result)
        self.assertNotIn("shares_source", result)
        self.assertNotIn(token, repr(result))

    def test_reported_raw_inputs_preserve_decimal_precision(self) -> None:
        raw_close = Decimal("1.234567890123456789")
        count = Decimal("1.000000000000000001")

        result = evidence(raw_close=raw_close, count=count)

        self.assertEqual(result["raw_close"], str(raw_close))
        self.assertEqual(result["shares_count"], str(count))

    def test_one_cent_over_ceiling_is_not_microcap(self) -> None:
        result = evidence(raw_close=3.0000000001, count=100_000_000)

        self.assertEqual(result["status"], "NOT_MICROCAP")
        self.assertAlmostEqual(result["raw_market_cap"], 300_000_000.01)

    def test_sub_float_precision_above_ceiling_stays_above_ceiling(self) -> None:
        cap = Decimal("300000000.00000000000000000001")
        result = evidence(raw_close=cap, count=1)

        self.assertEqual(result["status"], "NOT_MICROCAP")
        self.assertEqual(result["raw_market_cap"], str(cap))

    def test_huge_finite_decimal_fails_closed_instead_of_raising(self) -> None:
        for raw_close, count in (
            (Decimal("1e999999"), 1),
            (1, Decimal("1e999999")),
            (Decimal("1e999999"), Decimal("1e999999")),
        ):
            with self.subTest(raw_close=raw_close, count=count):
                result = evidence(raw_close=raw_close, count=count)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
                self.assertNotIn("raw_market_cap", result)

    def test_external_source_witness_is_required(self) -> None:
        result = evidence(source_verified=False)

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
        self.assertIn("source", result["reason"])

    def test_untrusted_source_claims_cannot_replace_external_witness(self) -> None:
        result = evidence(
            source_verified=False,
            price_overrides={"source_verified": True},
            share_overrides={"source_verified": True},
        )

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_future_filing_cannot_classify_past_decision(self) -> None:
        result = evidence(share_overrides={
            "filed_at": "2025-05-29T00:00:00Z",
            "available_at": "2025-05-29T00:00:00Z",
        })

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_missing_filing_or_availability_timestamp_is_unverified(self) -> None:
        for field in ("filed_at", "available_at"):
            with self.subTest(field=field):
                result = evidence(share_overrides={field: None})
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_future_availability_is_rejected_even_when_report_period_is_old(self) -> None:
        result = evidence(share_overrides={
            "available_at": "2025-05-28T15:00:01Z",
            "report_period_end": "2025-03-31",
        })

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_price_after_decision_or_older_than_five_minutes_is_unverified(self) -> None:
        cases = (
            {"known_at": "2025-05-28T15:00:01Z"},
            {"known_at": "2025-05-28T14:54:59Z"},
        )
        for overrides in cases:
            with self.subTest(known_at=overrides["known_at"]):
                result = evidence(price_overrides=overrides)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_price_exactly_five_minutes_old_is_eligible(self) -> None:
        result = evidence(price_overrides={"known_at": "2025-05-28T14:55:00Z"})

        self.assertEqual(result["status"], "MICROCAP")

    def test_price_and_shares_must_have_matching_raw_basis(self) -> None:
        for price_change, share_change in (
            ({"basis": "split-adjusted"}, {}),
            ({}, {"basis": "split-adjusted"}),
        ):
            with self.subTest(price_change=price_change, share_change=share_change):
                result = evidence(price_overrides=price_change, share_overrides=share_change)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_incomplete_or_multiple_common_classes_are_unverified(self) -> None:
        multiple_classes = [
            {
                "count": 50_000_000,
                "issuer_id": "issuer-1",
                "class_id": "common-a",
                "class_type": "common",
                "filed_at": "2025-05-28T14:00:00Z",
                "available_at": "2025-05-28T14:01:00Z",
                "basis": "raw",
                "source": "audited-filing",
            },
            {
                "count": 50_000_000,
                "issuer_id": "issuer-1",
                "class_id": "common-b",
                "class_type": "common",
                "filed_at": "2025-05-28T14:00:00Z",
                "available_at": "2025-05-28T14:01:00Z",
                "basis": "raw",
                "source": "audited-filing",
            },
        ]
        for complete, supplied_shares in (
            (False, None),
            (True, multiple_classes),
            (True, []),
        ):
            with self.subTest(complete=complete, shares=supplied_shares):
                result = classify_microcap(
                    issuer_id="issuer-1",
                    decision_at=DECISION_AT,
                    price={
                        "raw_close": 2.0,
                        "known_at": "2025-05-28T14:59:00Z",
                        "basis": "raw",
                        "issuer_id": "issuer-1",
                        "class_id": "common-a",
                        "class_type": "common",
                    },
                    shares=supplied_shares if supplied_shares is not None else [
                        {
                            "count": 100_000_000,
                            "issuer_id": "issuer-1",
                            "class_id": "common-a",
                            "class_type": "common",
                            "filed_at": "2025-05-28T14:00:00Z",
                            "available_at": "2025-05-28T14:01:00Z",
                            "basis": "raw",
                            "source": "audited-filing",
                        }
                    ],
                    classes_complete=complete,
                    corporate_actions_verified=True,
                    source_verified=True,
                )
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_price_class_and_issuer_identity_must_match(self) -> None:
        for changes in (
            {"issuer_id": "issuer-2"},
            {"class_id": "reused-ticker-class"},
        ):
            with self.subTest(changes=changes):
                result = evidence(price_overrides=changes)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

        result = evidence(share_overrides={"issuer_id": "issuer-2"})
        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_both_price_and_shares_need_explicit_common_class_proof(self) -> None:
        for price_change, share_change in (
            ({"class_type": None}, {"class_type": None}),
            (
                {"class_id": "preferred", "class_type": None},
                {"class_id": "preferred", "class_type": None},
            ),
            ({}, {"class_type": None}),
            ({"class_type": None}, {}),
            ({"class_type": "preferred"}, {"class_type": "common"}),
        ):
            with self.subTest(price_change=price_change, share_change=share_change):
                result = evidence(price_overrides=price_change, share_overrides=share_change)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_explicit_common_stock_class_is_eligible(self) -> None:
        result = evidence(
            price_overrides={"class_type": "common_stock"},
            share_overrides={"class_type": "common_stock"},
        )

        self.assertEqual(result["status"], "MICROCAP")

    def test_massive_ticker_details_shares_are_always_disallowed(self) -> None:
        result = evidence(share_overrides={
            "source": "massive_ticker_details",
            "filed_at": "2020-01-01T00:00:00Z",
            "available_at": "2020-01-01T00:00:00Z",
        })

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_massive_ambiguous_and_ticker_details_sources_are_disallowed(self) -> None:
        for source in (
            "massive",
            "MASSIVE",
            "massive.com",
            "Massive /v3/reference/tickers/SORA",
            "Massive ticker details derived from a filing",
        ):
            with self.subTest(source=source):
                result = evidence(share_overrides={"source": source})
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_filing_source_is_not_blocked_by_massive_name_alone(self) -> None:
        result = evidence(share_overrides={"source": "Massive SEC filing"})

        self.assertEqual(result["status"], "MICROCAP")

    def test_negative_and_non_finite_price_or_share_count_are_unverified(self) -> None:
        for raw_close, count in (
            (-1, 100),
            (1, -100),
            (0, 100),
            (1, 0),
            (float("nan"), 100),
            (1, float("nan")),
            (float("inf"), 100),
            (1, float("inf")),
        ):
            with self.subTest(raw_close=raw_close, count=count):
                result = evidence(raw_close=raw_close, count=count)
                self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
                self.assertNotIn("raw_market_cap", result)

    def test_missing_shares_never_becomes_zero(self) -> None:
        result = classify_microcap(
            issuer_id="issuer-1",
            decision_at=DECISION_AT,
            price={
                "raw_close": 1,
                "known_at": "2025-05-28T14:59:00Z",
                "basis": "raw",
                "issuer_id": "issuer-1",
                "class_id": "common",
                "class_type": "common",
            },
            shares=None,
            classes_complete=True,
            corporate_actions_verified=True,
            source_verified=True,
        )

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
        self.assertTrue(result["reason"])


if __name__ == "__main__":
    unittest.main()
