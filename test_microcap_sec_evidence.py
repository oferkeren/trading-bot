import unittest

from microcap_sec_evidence import observe_shares


CIK = "123"
ACCESSION = "0000000123-25-000001"
DECISION_AT = "2025-05-15T20:30:00Z"
FETCHED_AT = "2025-05-16T10:00:00Z"


def documents(
    *,
    cik: str = CIK,
    accession: str = ACCESSION,
    accepted: str = "2025-05-15T16:30:00-04:00",
    report_date: str = "2025-03-31",
    form: str = "10-Q",
    fact_rows: list[dict[str, object]] | None = None,
) -> tuple[dict[str, object], dict[str, object]]:
    submissions: dict[str, object] = {
        "cik": cik,
        "name": "ISSUER INC",
        "filings": {
            "recent": {
                "accessionNumber": [accession],
                "acceptanceDateTime": [accepted],
                "reportDate": [report_date],
                "form": [form],
            }
        },
    }
    facts: dict[str, object] = {
        "cik": cik,
        "entityName": "ISSUER INC",
        "facts": {
            "dei": {
                "EntityCommonStockSharesOutstanding": {
                    "units": {
                        "shares": fact_rows if fact_rows is not None else [
                            {
                                "accn": accession,
                                "end": report_date,
                                "val": 100_000_000,
                                "filed": "2025-05-15",
                            }
                        ]
                    }
                }
            }
        },
    }
    return submissions, facts


def observe(
    submissions: object,
    facts: object,
    *,
    cik: str = CIK,
    decision_at: str = DECISION_AT,
    fetched_at: str = FETCHED_AT,
    max_accessions: int = 2,
) -> dict[str, object]:
    return observe_shares(
        submissions, facts, cik=cik, decision_at=decision_at,
        fetched_at=fetched_at, max_accessions=max_accessions,
    )


class ObserveSharesTests(unittest.TestCase):
    def test_exact_acceptance_boundary_is_observed_but_never_verified(self):
        submissions, facts = documents()

        result = observe(submissions, facts)

        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")
        self.assertEqual(result["cik"], "0000000123")
        self.assertEqual(result["decision_at"], DECISION_AT)
        self.assertIs(result["source_verified"], False)
        self.assertEqual(result["coverage"], "UNVERIFIED")
        self.assertEqual(result["observations"], [{
            "accession": ACCESSION,
            "accepted_at": DECISION_AT,
            "report_date": "2025-03-31",
            "fetched_at": FETCHED_AT,
            "filing_class": "quarterly",
            "shares_count": 100_000_000,
        }])
        self.assertEqual(result["blockers"], sorted(result["blockers"]))
        self.assertNotIn("audited_witness", result)
        self.assertNotIn("classes_complete", result)
        self.assertNotIn("market_cap", result)
        self.assertNotIn("trade_approval", result)

    def test_current_report_form_is_observed_as_factual_evidence(self):
        submissions, facts = documents(form="8-K")

        result = observe(submissions, facts)

        self.assertEqual(result["observations"][0]["filing_class"], "current")
        self.assertNotIn("FILING_FORM_UNSUPPORTED", result["blockers"])
        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_current_report_amendment_is_flagged_and_classified_as_amendment(self):
        submissions, facts = documents(form="8-K/A")

        result = observe(submissions, facts)

        self.assertIn("AMENDMENT_PRESENT", result["blockers"])
        self.assertEqual(result["observations"][0]["filing_class"], "amendment")
        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_supported_original_forms_report_observed_count(self):
        for form, filing_class in (
            ("10-K", "annual"), ("10-Q", "quarterly"), ("8-K", "current"),
        ):
            with self.subTest(form=form):
                submissions, facts = documents(form=form)
                result = observe(submissions, facts)
                self.assertEqual(result["observations"][0]["filing_class"], filing_class)
                self.assertEqual(result["observations"][0]["shares_count"], 100_000_000)

    def test_unsupported_or_amended_forms_never_report_share_counts(self):
        cases = (
            ("S-1", "FILING_FORM_UNSUPPORTED"),
            ("10-K405", "FILING_FORM_UNSUPPORTED"),
            ("10-q", "FILING_FORM_UNSUPPORTED"),
            ("", "FILING_FORM_UNSUPPORTED"),
            ("10-K/A", "AMENDMENT_PRESENT"),
            ("10-Q/A", "AMENDMENT_PRESENT"),
            ("8-K/A", "AMENDMENT_PRESENT"),
        )
        for form, blocker in cases:
            with self.subTest(form=form):
                submissions, facts = documents(form=form)
                result = observe(submissions, facts)
                self.assertIn(blocker, result["blockers"])
                self.assertIsNone(result["observations"][0]["shares_count"])
                self.assertIs(result["source_verified"], False)

    def test_unsupported_form_only_suppresses_its_own_count(self):
        other = "0000000123-25-000002"
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["accessionNumber"].append(other)
        recent["acceptanceDateTime"].append("2025-03-01T10:00:00-05:00")
        recent["reportDate"].append("2024-12-31")
        recent["form"].append("S-1")
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"][
            "shares"
        ].append({
            "accn": other, "end": "2024-12-31", "val": 90_000_000,
            "filed": "2025-03-01",
        })

        result = observe(submissions, facts)

        self.assertIn("FILING_FORM_UNSUPPORTED", result["blockers"])
        self.assertEqual(
            [item["shares_count"] for item in result["observations"]],
            [100_000_000, None],
        )

    def test_non_string_form_never_reports_share_count(self):
        submissions, facts = documents()
        submissions["filings"]["recent"]["form"] = [None]

        result = observe(submissions, facts)

        self.assertIn("FILING_FORM_UNSUPPORTED", result["blockers"])
        self.assertIsNone(result["observations"][0]["shares_count"])

    def test_integrity_blockers_elsewhere_suppress_all_share_counts(self):
        other = "0000000123-25-000002"

        def two_filings(accepted, report_date, form, accession=other):
            submissions, facts = documents()
            recent = submissions["filings"]["recent"]
            recent["accessionNumber"].append(accession)
            recent["acceptanceDateTime"].append(accepted)
            recent["reportDate"].append(report_date)
            recent["form"].append(form)
            facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"][
                "shares"
            ].append({
                "accn": other, "end": "2024-12-31", "val": 90_000_000,
                "filed": "2025-03-01",
            })
            return submissions, facts

        cases = (
            ("AMENDMENT_PRESENT",
             two_filings("2025-05-15T10:00:00-04:00", "2025-03-31", "10-Q/A",
                         accession="0000000123-25-000009")),
            ("ACCEPTANCE_TIME_INVALID",
             two_filings("bad-time", "2024-12-31", "10-K")),
            ("REPORT_DATE_INVALID",
             two_filings("2025-03-01T10:00:00-05:00", "bad-date", "10-K")),
            ("SUBMISSIONS_ROW_INVALID",
             two_filings("2025-03-01T10:00:00-05:00", "2024-12-31", "10-K",
                         accession=ACCESSION)),
            ("SUBMISSIONS_ROW_INVALID",
             two_filings("2025-03-01T10:00:00-05:00", "2024-12-31", "10-K",
                         accession="malformed")),
        )
        for blocker, (submissions, facts) in cases:
            with self.subTest(blocker=blocker):
                result = observe(submissions, facts)
                self.assertIn(blocker, result["blockers"])
                self.assertTrue(result["observations"])
                self.assertTrue(all(
                    item["shares_count"] is None for item in result["observations"]
                ))

    def test_future_filing_does_not_suppress_earlier_valid_count(self):
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["accessionNumber"].insert(0, "0000000123-25-000002")
        recent["acceptanceDateTime"].insert(0, "2025-05-16T09:00:00-04:00")
        recent["reportDate"].insert(0, "2025-03-31")
        recent["form"].insert(0, "8-K")
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"][
            "shares"
        ].append({
            "accn": "0000000123-25-000002", "end": "2025-03-31",
            "val": 120_000_000, "filed": "2025-05-16",
        })

        result = observe(submissions, facts)

        self.assertIn("FUTURE_ACCEPTANCE", result["blockers"])
        self.assertEqual(
            [item["shares_count"] for item in result["observations"]], [100_000_000]
        )

    def test_future_joined_amendment_does_not_block_earlier_historical_count(self):
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["accessionNumber"].insert(0, "0000000123-25-000002")
        recent["acceptanceDateTime"].insert(0, "2025-05-16T09:00:00-04:00")
        recent["reportDate"].insert(0, "2025-03-31")
        recent["form"].insert(0, "10-Q/A")
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"][
            "shares"
        ].append({
            "accn": "0000000123-25-000002", "end": "2025-03-31",
            "val": 120_000_000, "filed": "2025-05-16",
        })

        result = observe(submissions, facts)

        self.assertIn("FUTURE_ACCEPTANCE", result["blockers"])
        self.assertNotIn("AMENDMENT_PRESENT", result["blockers"])
        self.assertEqual(
            [item["shares_count"] for item in result["observations"]], [100_000_000]
        )

    def test_later_acceptance_is_not_made_available_by_an_old_report_date(self):
        submissions, facts = documents(
            accepted="2025-05-15T16:30:01-04:00",
            report_date="2025-03-31",
        )

        result = observe(submissions, facts)

        self.assertIn("FUTURE_ACCEPTANCE", result["blockers"])
        self.assertEqual(result["observations"], [])

    def test_date_only_or_missing_acceptance_time_is_invalid(self):
        for accepted in ("2025-05-15", None):
            with self.subTest(accepted=accepted):
                submissions, facts = documents()
                submissions["filings"]["recent"]["acceptanceDateTime"] = [accepted]

                result = observe(submissions, facts)

                self.assertIn("ACCEPTANCE_TIME_INVALID", result["blockers"])
                self.assertEqual(result["observations"], [])

    def test_naive_sec_acceptance_uses_eastern_daylight_time(self):
        submissions, facts = documents(
            accepted="2025-03-10T09:00:00",
            report_date="2025-02-28",
        )

        result = observe(
            submissions, facts, decision_at="2025-03-10T13:00:00Z"
        )

        self.assertEqual(result["observations"][0]["accepted_at"], "2025-03-10T13:00:00Z")

    def test_ambiguous_or_nonexistent_eastern_dst_acceptance_is_rejected(self):
        for accepted in ("2025-11-02T01:30:00", "2025-03-09T02:30:00"):
            with self.subTest(accepted=accepted):
                submissions, facts = documents(accepted=accepted)
                result = observe(submissions, facts)
                self.assertIn("ACCEPTANCE_TIME_INVALID", result["blockers"])
                self.assertEqual(result["observations"], [])
                self.assertNotIn(accepted, repr(result))

    def test_explicit_acceptance_timezone_is_required_when_not_sec_local_time(self):
        submissions, facts = documents(accepted="2025-05-15T16:30:00 PST")

        result = observe(submissions, facts)

        self.assertIn("ACCEPTANCE_TIME_INVALID", result["blockers"])
        self.assertEqual(result["observations"], [])
        self.assertNotIn("PST", repr(result))

    def test_amendment_is_a_blocker_and_is_not_reported_as_ordinary_filing(self):
        submissions, facts = documents(form="10-Q/A")

        result = observe(submissions, facts)

        self.assertIn("AMENDMENT_PRESENT", result["blockers"])
        self.assertEqual(result["observations"][0]["filing_class"], "amendment")
        self.assertEqual(result["status"], "MARKET_CAP_UNVERIFIED")

    def test_amendment_blocks_even_when_no_companyfacts_row_joins_it(self):
        submissions, facts = documents(form="10-Q/A", fact_rows=[])

        result = observe(submissions, facts)

        self.assertIn("AMENDMENT_PRESENT", result["blockers"])
        self.assertEqual(result["observations"], [])

    def test_multiple_share_classes_are_blocked_without_copying_class_labels(self):
        submissions, facts = documents(fact_rows=[
            {
                "accn": ACCESSION, "end": "2025-03-31", "val": 50_000_000,
                "filed": "2025-05-15", "class": "Class A secret",
            },
            {
                "accn": ACCESSION, "end": "2025-03-31", "val": 50_000_000,
                "filed": "2025-05-15", "class": "Class B secret",
            },
        ])

        result = observe(submissions, facts)

        self.assertIn("AMBIGUOUS_CLASS_COVERAGE", result["blockers"])
        self.assertNotIn("Class A secret", repr(result))
        self.assertNotIn("Class B secret", repr(result))
        self.assertNotIn("classes_complete", result)

    def test_mismatched_or_reused_cik_is_rejected(self):
        submissions, facts = documents()
        facts["cik"] = "999"

        result = observe(submissions, facts)

        self.assertIn("CIK_MISMATCH", result["blockers"])
        self.assertEqual(result["observations"], [])
        self.assertNotIn("ISSUER INC", repr(result))

    def test_accession_cik_must_match_requested_cik_before_count_is_accepted(self):
        mismatched_accession = "0000000999-25-000001"
        submissions, facts = documents(accession=mismatched_accession)
        facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"][0][
            "accn"
        ] = mismatched_accession

        result = observe(submissions, facts)

        self.assertIn("ACCESSION_CIK_MISMATCH", result["blockers"])
        self.assertFalse(any(
            isinstance(item["shares_count"], int) and item["shares_count"] > 0
            for item in result["observations"]
        ))

    def test_malformed_companyfacts_rows_block_positive_counts(self):
        valid_row = {
            "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
            "filed": "2025-05-15",
        }
        malformed_rows = (
            {"unexpected": [valid_row]},
            [valid_row, None],
            [valid_row, {"end": "2025-03-31", "val": 50_000_000}],
            [valid_row, {
                "accn": "not-an-accession", "end": "2025-03-31",
                "val": 50_000_000,
            }],
        )
        for rows in malformed_rows:
            with self.subTest(rows=rows):
                submissions, facts = documents()
                facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"][
                    "shares"
                ] = rows

                result = observe(submissions, facts)

                self.assertIn("FACT_ROWS_INVALID", result["blockers"])
                self.assertFalse(any(
                    isinstance(item["shares_count"], int) and item["shares_count"] > 0
                    for item in result["observations"]
                ))
                self.assertLessEqual(len(result["observations"]), 2)

    def test_cik_values_must_be_digit_identifiers_not_coerced(self):
        submissions, facts = documents()
        submissions["cik"] = "issuer-123"

        result = observe(submissions, facts)

        self.assertIn("CIK_MISMATCH", result["blockers"])
        self.assertEqual(result["observations"], [])
        self.assertNotIn("issuer-123", repr(result))

    def test_no_accession_join_is_explicitly_unverified(self):
        submissions, facts = documents(fact_rows=[{
            "accn": "0000000123-25-000002", "end": "2025-03-31",
            "val": 100_000_000, "filed": "2025-05-15",
        }])

        result = observe(
            submissions, facts, cik="123",
            decision_at="2025-05-15T16:30:00-04:00",
        )

        self.assertIn("SHARES_FACT_ABSENT", result["blockers"])
        self.assertEqual(result["coverage"], "UNVERIFIED")
        self.assertEqual(result["observations"], [])
        self.assertEqual(result["cik"], "0000000123")
        self.assertEqual(result["decision_at"], DECISION_AT)

    def test_nonpositive_and_malformed_share_counts_are_never_returned(self):
        for value in (-1, 0, 1.5, "100000000", True, None):
            with self.subTest(value=value):
                submissions, facts = documents(fact_rows=[{
                    "accn": ACCESSION, "end": "2025-03-31",
                    "val": value, "filed": "2025-05-15",
                }])
                result = observe(submissions, facts)
                self.assertIn("SHARES_COUNT_INVALID", result["blockers"])
                self.assertIsNone(result["observations"][0]["shares_count"])
                self.assertNotIn("val", result["observations"][0])

    def test_contradictory_counts_for_one_accession_are_blocked(self):
        submissions, facts = documents(fact_rows=[
            {
                "accn": ACCESSION, "end": "2025-03-31", "val": 50_000_000,
                "filed": "2025-05-15",
            },
            {
                "accn": ACCESSION, "end": "2025-03-31", "val": 60_000_000,
                "filed": "2025-05-15",
            },
        ])

        result = observe(submissions, facts)

        self.assertIn("CONTRADICTORY_FACTS", result["blockers"])
        self.assertIsNone(result["observations"][0]["shares_count"])

    def test_any_bad_end_in_joined_accession_blocks_its_share_count(self):
        for bad_end in ("not-a-date", None, "2025-03-30"):
            with self.subTest(bad_end=bad_end):
                bad_row = {
                    "accn": ACCESSION, "val": 50_000_000, "filed": "2025-05-15",
                }
                if bad_end is not None:
                    bad_row["end"] = bad_end
                submissions, facts = documents(fact_rows=[
                    {
                        "accn": ACCESSION, "end": "2025-03-31",
                        "val": 100_000_000, "filed": "2025-05-15",
                    },
                    bad_row,
                ])

                result = observe(submissions, facts)

                self.assertIn("REPORT_DATE_MISMATCH", result["blockers"])
                self.assertIsNone(result["observations"][0]["shares_count"])
                self.assertLessEqual(len(result["observations"]), 2)

    def test_any_bad_filed_date_in_joined_accession_blocks_its_share_count(self):
        for bad_filed in ("not-a-date", None, "2025-05-16"):
            with self.subTest(bad_filed=bad_filed):
                bad_row = {
                    "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
                }
                if bad_filed is not None:
                    bad_row["filed"] = bad_filed
                submissions, facts = documents(fact_rows=[
                    {
                        "accn": ACCESSION, "end": "2025-03-31",
                        "val": 100_000_000, "filed": "2025-05-15",
                    },
                    bad_row,
                ])

                result = observe(submissions, facts)

                expected_blocker = (
                    "FILED_DATE_INVALID" if bad_filed is None or bad_filed == "not-a-date"
                    else "FILED_DATE_MISMATCH"
                )
                self.assertIn(expected_blocker, result["blockers"])
                self.assertIsNone(result["observations"][0]["shares_count"])
                self.assertLessEqual(len(result["observations"]), 2)

    def test_malformed_filing_arrays_are_not_partially_zipped(self):
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["form"] = []

        result = observe(submissions, facts)

        self.assertIn("SUBMISSIONS_ARRAYS_INVALID", result["blockers"])
        self.assertEqual(result["observations"], [])

    def test_invalid_provider_dates_are_not_echoed(self):
        submissions, facts = documents(accepted="not-a-date-secret")

        result = observe(submissions, facts)

        self.assertIn("ACCEPTANCE_TIME_INVALID", result["blockers"])
        self.assertNotIn("not-a-date-secret", repr(result))

    def test_file_date_is_only_a_cross_check_not_the_availability_timestamp(self):
        submissions, facts = documents(fact_rows=[{
            "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
            "filed": "2025-05-16",
        }])

        result = observe(submissions, facts)

        self.assertIn("FILED_DATE_MISMATCH", result["blockers"])
        self.assertEqual(result["observations"][0]["accepted_at"], DECISION_AT)

    def test_after_hours_thursday_acceptance_allows_friday_filed_date(self):
        submissions, facts = documents(
            accepted="2025-05-15T18:08:00-04:00",
            fact_rows=[{
                "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-16",
            }],
        )

        result = observe(submissions, facts, decision_at="2025-05-15T22:08:00Z")

        self.assertNotIn("FILED_DATE_MISMATCH", result["blockers"])
        self.assertEqual(result["observations"][0]["shares_count"], 100_000_000)

    def test_after_hours_friday_acceptance_allows_monday_filed_date(self):
        submissions, facts = documents(
            accepted="2025-05-16T18:00:00-04:00",
            fact_rows=[{
                "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-19",
            }],
        )

        result = observe(submissions, facts, decision_at="2025-05-16T22:00:00Z")

        self.assertNotIn("FILED_DATE_MISMATCH", result["blockers"])
        self.assertEqual(result["observations"][0]["shares_count"], 100_000_000)

    def test_before_after_hours_cutoff_rejects_next_day_filed_date(self):
        submissions, facts = documents(
            accepted="2025-05-15T17:29:00-04:00",
            fact_rows=[{
                "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-16",
            }],
        )

        result = observe(submissions, facts, decision_at="2025-05-15T21:29:00Z")

        self.assertIn("FILED_DATE_MISMATCH", result["blockers"])
        self.assertIsNone(result["observations"][0]["shares_count"])

    def test_after_hours_acceptance_rejects_filed_date_two_business_days_later(self):
        submissions, facts = documents(
            accepted="2025-05-15T18:08:00-04:00",
            fact_rows=[{
                "accn": ACCESSION, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-19",
            }],
        )

        result = observe(submissions, facts, decision_at="2025-05-15T22:08:00Z")

        self.assertIn("FILED_DATE_MISMATCH", result["blockers"])
        self.assertIsNone(result["observations"][0]["shares_count"])

    def test_observations_are_capped_at_two_accessions(self):
        accessions = [
            "0000000123-25-000001",
            "0000000123-25-000002",
            "0000000123-25-000003",
        ]
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["accessionNumber"] = accessions
        recent["acceptanceDateTime"] = [
            "2025-05-15T16:30:00-04:00",
            "2025-05-14T16:30:00-04:00",
            "2025-05-13T16:30:00-04:00",
        ]
        recent["reportDate"] = ["2025-03-31"] * 3
        recent["form"] = ["10-Q"] * 3
        shares = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"]["units"]["shares"]
        shares.extend(
            {
                "accn": accession, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-15",
            }
            for accession in accessions[1:]
        )

        result = observe(submissions, facts, max_accessions=99)

        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(
            [item["accession"] for item in result["observations"]],
            accessions[:2],
        )
        self.assertIs(result["coverage_truncated"], True)

    def test_malformed_third_joined_metadata_suppresses_observed_counts(self):
        accessions = [
            "0000000123-25-000001",
            "0000000123-25-000002",
            "0000000123-25-000003",
        ]
        malformed_metadata = (
            (
                ["2025-05-15T16:30:00-04:00", "2025-05-14T16:30:00-04:00", "bad-time"],
                ["2025-03-31"] * 3,
                "ACCEPTANCE_TIME_INVALID",
            ),
            (
                ["2025-05-15T16:30:00-04:00", "2025-05-14T16:30:00-04:00",
                 "2025-05-13T16:30:00-04:00"],
                ["2025-03-31", "2025-03-31", "bad-date"],
                "REPORT_DATE_INVALID",
            ),
        )

        for accepted_values, report_dates, blocker in malformed_metadata:
            with self.subTest(blocker=blocker):
                submissions, facts = documents()
                recent = submissions["filings"]["recent"]
                recent["accessionNumber"] = accessions
                recent["acceptanceDateTime"] = accepted_values
                recent["reportDate"] = report_dates
                recent["form"] = ["10-Q"] * 3
                shares = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"][
                    "units"
                ]["shares"]
                shares.extend(
                    {
                        "accn": accession, "end": "2025-03-31",
                        "val": 100_000_000, "filed": "2025-05-15",
                    }
                    for accession in accessions[1:]
                )

                result = observe(submissions, facts)

                self.assertIn(blocker, result["blockers"])
                self.assertEqual(len(result["observations"]), 2)
                self.assertTrue(all(
                    item["shares_count"] is None for item in result["observations"]
                ))
                self.assertIs(result["coverage_truncated"], True)

    def test_valid_third_joined_accession_is_marked_truncated_without_emission(self):
        accessions = [
            "0000000123-25-000001",
            "0000000123-25-000002",
            "0000000123-25-000003",
        ]
        submissions, facts = documents()
        recent = submissions["filings"]["recent"]
        recent["accessionNumber"] = accessions
        recent["acceptanceDateTime"] = [
            "2025-05-15T16:30:00-04:00",
            "2025-05-14T16:30:00-04:00",
            "2025-05-13T16:30:00-04:00",
        ]
        recent["reportDate"] = ["2025-03-31"] * 3
        recent["form"] = ["10-Q"] * 3
        shares = facts["facts"]["dei"]["EntityCommonStockSharesOutstanding"][
            "units"
        ]["shares"]
        shares.extend(
            {
                "accn": accession, "end": "2025-03-31", "val": 100_000_000,
                "filed": "2025-05-15",
            }
            for accession in accessions[1:]
        )

        result = observe(submissions, facts)

        self.assertEqual(len(result["observations"]), 2)
        self.assertEqual(
            [item["accession"] for item in result["observations"]],
            accessions[:2],
        )
        self.assertIs(result["coverage_truncated"], True)


if __name__ == "__main__":
    unittest.main()
