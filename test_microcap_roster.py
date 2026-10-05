import copy
import unittest

from microcap_history import CoverageError
from microcap_roster import eligible_members, member_for_issuer


def member(
    issuer_id: str,
    symbol: str,
    valid_from: str,
    valid_to: str | None,
    listing_status: str,
    source_record_id: str,
) -> dict[str, str | None]:
    return {
        "issuer_id": issuer_id,
        "symbol": symbol,
        "company": f"{issuer_id} Company",
        "valid_from": valid_from,
        "valid_to": valid_to,
        "listing_status": listing_status,
        "source_record_id": source_record_id,
    }


def roster() -> dict[str, object]:
    return {
        "source_url": "https://example.com/historical-members",
        "retrieved_at": "2024-01-01T12:00:00Z",
        "coverage_claim": "historical-listed-and-delisted",
        "members": [
            member("issuer-1", "OLD", "2020-01-01T00:00:00Z",
                   "2022-01-01T00:00:00Z", "delisted", "record-old"),
            member("issuer-1", "NEW", "2022-01-01T00:00:00Z",
                   None, "listed", "record-new"),
        ],
    }


class EligibleMembersTests(unittest.TestCase):
    def test_member_for_issuer_rejects_concurrent_symbols_regardless_of_order(self) -> None:
        for order in (("AAA", "BBB"), ("BBB", "AAA")):
            with self.subTest(order=order):
                data = roster()
                data["members"] = [
                    member("issuer-1", symbol, "2020-01-01T00:00:00Z", None, "listed", f"record-{symbol}")
                    for symbol in order
                ]
                for allow_pilot in (False, True):
                    if allow_pilot:
                        data["coverage_claim"] = "pilot-unverified"
                    with self.assertRaisesRegex(CoverageError, "^CONTRACT_UNRESOLVED"):
                        member_for_issuer(data, "issuer-1", "2021-06-15T12:00:00Z",
                                          allow_pilot=allow_pilot)

    def test_member_for_issuer_returns_single_active_symbol_or_none(self) -> None:
        data = roster()
        resolved = member_for_issuer(data, "issuer-1", "2021-06-15T12:00:00Z")
        self.assertIsNotNone(resolved)
        self.assertEqual(resolved["symbol"], "OLD")
        self.assertIsNone(member_for_issuer(data, "issuer-2", "2021-06-15T12:00:00Z"))

    def test_pilot_claim_requires_explicit_opt_in_and_preserves_provenance(self) -> None:
        data = roster()
        data["coverage_claim"] = "pilot-unverified"

        with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
            eligible_members(data, "2021-06-15T12:00:00Z")
        resolved = eligible_members(data, "2021-06-15T12:00:00Z", allow_pilot=True)
        self.assertEqual([item["symbol"] for item in resolved], ["OLD"])
        self.assertEqual(resolved[0]["coverage_claim"], "pilot-unverified")
        self.assertEqual(resolved[0]["at"], "2021-06-15T12:00:00Z")

    def test_pilot_claim_uses_full_member_and_provenance_validation(self) -> None:
        for mutation, code in (
            (lambda data: data.update(source_url="file:///private"), "ROSTER_UNVERIFIED"),
            (lambda data: data["members"][0].update(valid_from="bad"), "ROSTER_INVALID"),
            (lambda data: data["members"][1].update(source_record_id="record-old"), "ROSTER_INVALID"),
            (lambda data: data["members"][1].update(
                symbol="OLD", valid_from="2021-01-01T00:00:00Z"), "ROSTER_INVALID"),
        ):
            with self.subTest(code=code, mutation=mutation):
                data = roster()
                data["coverage_claim"] = "pilot-unverified"
                mutation(data)
                with self.assertRaisesRegex(CoverageError, code):
                    eligible_members(data, "2021-06-15T12:00:00Z", allow_pilot=True)

    def test_returns_the_membership_valid_at_each_date(self) -> None:
        data = roster()

        old = eligible_members(data, "2021-06-15T12:00:00Z")
        new = eligible_members(data, "2023-06-15T12:00:00Z")

        self.assertEqual([item["symbol"] for item in old], ["OLD"])
        self.assertEqual([item["symbol"] for item in new], ["NEW"])
        self.assertEqual(old[0]["listing_status"], "delisted")
        self.assertEqual(old[0]["source_url"], data["source_url"])
        self.assertEqual(old[0]["retrieved_at"], data["retrieved_at"])
        self.assertEqual(old[0]["coverage_claim"], data["coverage_claim"])
        self.assertEqual(old[0]["at"], "2021-06-15T12:00:00Z")

    def test_validity_windows_are_half_open(self) -> None:
        data = roster()

        self.assertEqual(
            [item["symbol"] for item in eligible_members(data, "2022-01-01T00:00:00Z")],
            ["NEW"],
        )

    def test_missing_provenance_is_unverified(self) -> None:
        data = roster()
        data["source_url"] = ""

        with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
            eligible_members(data, "2021-06-15T12:00:00Z")

    def test_missing_members_or_inexact_coverage_claim_is_unverified(self) -> None:
        for field, value in (
            ("members", []),
            ("coverage_claim", "current-listed-only"),
        ):
            with self.subTest(field=field):
                data = roster()
                data[field] = value

                with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
                    eligible_members(data, "2021-06-15T12:00:00Z")

    def test_rejects_malformed_timestamps(self) -> None:
        for field, value in (
            ("retrieved_at", "not-a-timestamp"),
            ("at", "2021-06-15"),
            ("valid_from", "2020-01-01"),
            ("valid_to", "not-a-timestamp"),
        ):
            with self.subTest(field=field):
                data = roster()
                if field == "at":
                    at = value
                else:
                    at = "2021-06-15T12:00:00Z"
                    if field == "retrieved_at":
                        data["retrieved_at"] = value
                    else:
                        records = copy.deepcopy(data["members"])
                        records[0][field] = value
                        data["members"] = records

                with self.assertRaises(CoverageError):
                    eligible_members(data, at)

    def test_rejects_duplicate_source_record_ids(self) -> None:
        data = roster()
        records = copy.deepcopy(data["members"])
        records[1]["source_record_id"] = records[0]["source_record_id"]
        data["members"] = records

        with self.assertRaisesRegex(CoverageError, "ROSTER_INVALID"):
            eligible_members(data, "2021-06-15T12:00:00Z")

    def test_rejects_overlapping_issuer_symbol_windows(self) -> None:
        data = roster()
        records = copy.deepcopy(data["members"])
        records.append(
            member("issuer-1", "OLD", "2021-12-01T00:00:00Z",
                   "2023-01-01T00:00:00Z", "delisted", "record-overlap")
        )
        data["members"] = records

        with self.assertRaisesRegex(CoverageError, "ROSTER_INVALID"):
            eligible_members(data, "2021-12-15T00:00:00Z")

    def test_rejects_symbol_mapped_to_two_issuers_on_same_date(self) -> None:
        data = roster()
        records = copy.deepcopy(data["members"])
        records.append(
            member("issuer-2", "old", "2021-01-01T00:00:00Z",
                   "2021-12-01T00:00:00Z", "delisted", "record-other-issuer")
        )
        data["members"] = records

        with self.assertRaisesRegex(CoverageError, "ROSTER_INVALID"):
            eligible_members(data, "2021-06-15T00:00:00Z")

    def test_rejects_symbols_with_surrounding_whitespace(self) -> None:
        data = roster()
        records = copy.deepcopy(data["members"])
        records[0]["symbol"] = "ABC"
        records.append(
            member("issuer-2", " ABC ", "2021-01-01T00:00:00Z",
                   "2021-12-01T00:00:00Z", "delisted", "record-whitespace")
        )
        data["members"] = records

        with self.assertRaisesRegex(CoverageError, "ROSTER_INVALID"):
            eligible_members(data, "2021-06-15T00:00:00Z")

    def test_allows_symbol_reassignment_at_a_nonoverlapping_boundary(self) -> None:
        data = roster()
        records = copy.deepcopy(data["members"])
        records.append(
            member("issuer-2", "OLD", "2022-01-01T00:00:00Z",
                   "2023-01-01T00:00:00Z", "delisted", "record-reassigned")
        )
        data["members"] = records

        eligible = eligible_members(data, "2022-06-15T00:00:00Z")

        self.assertEqual([item["symbol"] for item in eligible], ["NEW", "OLD"])

    def test_rejects_unknown_source_format(self) -> None:
        for url in ("file:///etc/passwd", "https://", "https://@example.com/roster"):
            with self.subTest(url=url):
                data = roster()
                data["source_url"] = url

                with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
                    eligible_members(data, "2021-06-15T12:00:00Z")

    def test_rejects_source_urls_with_query_or_fragment(self) -> None:
        for url in (
            "https://example.com/historical-members?access_token=secret",
            "https://example.com/historical-members?",
            "https://example.com/historical-members#private-fragment",
            "https://example.com/historical-members#",
        ):
            with self.subTest(url=url):
                data = roster()
                data["source_url"] = url

                with self.assertRaisesRegex(CoverageError, "ROSTER_UNVERIFIED"):
                    eligible_members(data, "2021-06-15T12:00:00Z")

    def test_timestamp_overflow_uses_roster_error_conventions(self) -> None:
        cases = (
            ("at", "0001-01-01T00:00:00+01:00", "TIMESTAMP_INVALID"),
            ("retrieved_at", "0001-01-01T00:00:00+01:00", "ROSTER_UNVERIFIED"),
            ("valid_from", "0001-01-01T00:00:00+01:00", "ROSTER_INVALID"),
            ("valid_to", "9999-12-31T23:59:59-01:00", "ROSTER_INVALID"),
        )
        for field, value, error_code in cases:
            with self.subTest(field=field):
                data = roster()
                at = "2021-06-15T12:00:00Z"
                if field == "at":
                    at = value
                elif field == "retrieved_at":
                    data["retrieved_at"] = value
                else:
                    records = copy.deepcopy(data["members"])
                    records[0][field] = value
                    data["members"] = records

                with self.assertRaisesRegex(CoverageError, error_code):
                    eligible_members(data, at)

    def test_rejects_dates_without_membership(self) -> None:
        with self.assertRaisesRegex(CoverageError, "ROSTER_NO_MEMBERS_AT_DATE"):
            eligible_members(roster(), "2019-12-31T23:59:59Z")


if __name__ == "__main__":
    unittest.main()
