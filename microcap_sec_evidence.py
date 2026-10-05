"""Fail-closed, observation-only joins of SEC submissions and company facts."""

from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo


_ACCESSION_PATTERN = re.compile(r"[0-9]{10}-[0-9]{2}-[0-9]{6}\Z")
_DATE_PATTERN = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}\Z")
_SEC_EASTERN = ZoneInfo("America/New_York")
_MAX_OBSERVATIONS = 2


# Issuer-level integrity failures that make every observed count unsafe.
_COUNT_SUPPRESSING_BLOCKERS = frozenset({
    "ACCEPTANCE_TIME_INVALID",
    "ACCESSION_CIK_MISMATCH",
    "AMENDMENT_PRESENT",
    "FACT_ROWS_INVALID",
    "REPORT_DATE_INVALID",
    "SUBMISSIONS_ROW_INVALID",
})


def _mapping(value: object) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _cik(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    text = str(value)
    if not 1 <= len(text) <= 10 or not text.isascii() or not text.isdecimal():
        return None
    return text.zfill(10)


def _timestamp(value: object, *, sec_local: bool = False) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    if _DATE_PATTERN.fullmatch(value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not sec_local:
            return None
        first = parsed.replace(tzinfo=_SEC_EASTERN, fold=0)
        second = parsed.replace(tzinfo=_SEC_EASTERN, fold=1)
        if first.utcoffset() != second.utcoffset():
            return None
        utc_value = first.astimezone(timezone.utc)
        if utc_value.astimezone(_SEC_EASTERN).replace(tzinfo=None) != parsed:
            return None
        parsed = first
    try:
        if parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _date(value: object) -> date | None:
    if not isinstance(value, str) or not _DATE_PATTERN.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def _timestamp_text(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _filing_class(value: object) -> str:
    if not isinstance(value, str):
        return "other"
    if value in ("10-K/A", "10-Q/A", "8-K/A"):
        return "amendment"
    if value == "10-K":
        return "annual"
    if value == "10-Q":
        return "quarterly"
    if value == "8-K":
        return "current"
    return "other"


def _normalized_name(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    return " ".join(value.split()).casefold()


def observe_shares(
    submissions: Mapping,
    companyfacts: Mapping,
    *,
    cik: str,
    decision_at: str,
    fetched_at: str,
    max_accessions: int = 2,
) -> dict[str, object]:
    """Return bounded SEC filing observations without asserting share coverage.

    Naive SEC acceptance timestamps are interpreted as America/New_York local
    time, including DST validation; timestamps with explicit offsets use that
    offset. Company-facts ``filed`` dates are consistency checks only.
    """
    blockers: set[str] = {"CLASS_COVERAGE_UNVERIFIED"}
    observations: list[dict[str, object]] = []
    requested_cik = _cik(cik)
    decision = _timestamp(decision_at)
    result: dict[str, object] = {
        "status": "MARKET_CAP_UNVERIFIED",
        "source_verified": False,
        "coverage": "UNVERIFIED",
        "coverage_truncated": False,
        "cik": requested_cik,
        "decision_at": _timestamp_text(decision) if decision is not None else None,
        "observations": observations,
        "blockers": [],
    }

    submissions_cik = _cik(_mapping(submissions).get("cik"))
    facts_cik = _cik(_mapping(companyfacts).get("cik"))
    if (
        requested_cik is None
        or submissions_cik != requested_cik
        or facts_cik != requested_cik
    ):
        blockers.add("CIK_MISMATCH")
        result["blockers"] = sorted(blockers)
        return result

    submissions_name = _normalized_name(_mapping(submissions).get("name"))
    facts_name = _normalized_name(_mapping(companyfacts).get("entityName"))
    if (
        submissions_name is not None
        and facts_name is not None
        and submissions_name != facts_name
    ):
        blockers.add("ISSUER_MISMATCH")
        result["blockers"] = sorted(blockers)
        return result

    fetched = _timestamp(fetched_at)
    if decision is None:
        blockers.add("DECISION_TIME_INVALID")
    if fetched is None:
        blockers.add("FETCH_TIME_INVALID")
    if decision is None or fetched is None:
        result["blockers"] = sorted(blockers)
        return result

    if isinstance(max_accessions, bool) or not isinstance(max_accessions, int) or max_accessions < 1:
        blockers.add("ACCESSION_LIMIT_INVALID")
        result["blockers"] = sorted(blockers)
        return result
    limit = min(max_accessions, _MAX_OBSERVATIONS)

    recent = _mapping(_mapping(_mapping(submissions).get("filings")).get("recent"))
    fields = (
        recent.get("accessionNumber"),
        recent.get("acceptanceDateTime"),
        recent.get("reportDate"),
        recent.get("form"),
    )
    if any(not isinstance(field, list) for field in fields):
        blockers.add("SUBMISSIONS_ARRAYS_INVALID")
        result["blockers"] = sorted(blockers)
        return result
    lengths = {len(field) for field in fields}
    if len(lengths) != 1:
        blockers.add("SUBMISSIONS_ARRAYS_INVALID")
        result["blockers"] = sorted(blockers)
        return result
    for accepted_value, form_value in zip(fields[1], fields[3]):
        if _filing_class(form_value) == "amendment":
            accepted = _timestamp(accepted_value, sec_local=True)
            if accepted is None:
                blockers.add("ACCEPTANCE_TIME_INVALID")
            elif accepted <= decision:
                blockers.add("AMENDMENT_PRESENT")

    units = _mapping(
        _mapping(
            _mapping(
                _mapping(
                    _mapping(companyfacts).get("facts")
                ).get("dei")
            ).get("EntityCommonStockSharesOutstanding")
        ).get("units")
    )
    fact_rows = units.get("shares")
    if not isinstance(fact_rows, list):
        fact_rows = []
        blockers.add("FACT_ROWS_INVALID")
        blockers.add("SHARES_FACT_ABSENT")

    facts_by_accession: dict[str, list[Mapping]] = {}
    invalid_fact_rows = False
    for row in fact_rows:
        if not isinstance(row, Mapping):
            invalid_fact_rows = True
            blockers.add("FACT_ROWS_INVALID")
            continue
        accession = row.get("accn")
        if not isinstance(accession, str) or not _ACCESSION_PATTERN.fullmatch(accession):
            invalid_fact_rows = True
            blockers.add("FACT_ROWS_INVALID")
            continue
        if _cik(accession[:10]) != requested_cik:
            blockers.add("ACCESSION_CIK_MISMATCH")
            continue
        facts_by_accession.setdefault(accession, []).append(row)

    matching_records = 0
    visited_accessions: set[str] = set()
    for accession_value, accepted_value, report_value, form_value in zip(*fields):
        if (
            not isinstance(accession_value, str)
            or not _ACCESSION_PATTERN.fullmatch(accession_value)
        ):
            blockers.add("SUBMISSIONS_ROW_INVALID")
            continue
        matching_rows = facts_by_accession.get(accession_value)
        if not matching_rows:
            continue
        if accession_value in visited_accessions:
            blockers.add("SUBMISSIONS_ROW_INVALID")
            continue
        visited_accessions.add(accession_value)
        matching_records += 1
        over_cap = len(observations) >= limit

        accepted = _timestamp(accepted_value, sec_local=True)
        report_date = _date(report_value)
        filing_class = _filing_class(form_value)
        if accepted is None:
            blockers.add("ACCEPTANCE_TIME_INVALID")
        elif accepted > decision:
            blockers.add("FUTURE_ACCEPTANCE")
        if report_date is None:
            blockers.add("REPORT_DATE_INVALID")
        # Amended filings are flagged separately and never yield a count:
        # whether they restate or supersede the original fact is unverified.
        unsupported_form = filing_class in ("amendment", "other")
        if filing_class == "amendment":
            if accepted is not None and accepted <= decision:
                blockers.add("AMENDMENT_PRESENT")
        elif filing_class == "other":
            blockers.add("FILING_FORM_UNSUPPORTED")
        if over_cap:
            result["coverage_truncated"] = True
        if (
            accepted is None
            or accepted > decision
            or report_date is None
            or over_cap
        ):
            continue

        dated_rows: list[Mapping] = []
        invalid_required_dates = False
        accepted_eastern_date = accepted.astimezone(_SEC_EASTERN).date()
        for row in matching_rows:
            fact_end = _date(row.get("end"))
            if fact_end == report_date:
                dated_rows.append(row)
            else:
                blockers.add("REPORT_DATE_MISMATCH")
                invalid_required_dates = True
            filed_date = _date(row.get("filed"))
            if filed_date is None:
                blockers.add("FILED_DATE_INVALID")
                invalid_required_dates = True
            elif filed_date != accepted_eastern_date:
                blockers.add("FILED_DATE_MISMATCH")
                invalid_required_dates = True
        if not dated_rows:
            blockers.add("SHARES_FACT_ABSENT")

        count_values: set[int] = set()
        invalid_count = False
        class_values: set[str] = set()
        for row in dated_rows:
            value = row.get("val")
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                invalid_count = True
                blockers.add("SHARES_COUNT_INVALID")
            else:
                count_values.add(value)
            for field in ("class", "classOfStock", "shareClass"):
                class_value = row.get(field)
                if isinstance(class_value, str) and class_value.strip():
                    class_values.add(class_value.strip())

        if len(count_values) > 1:
            blockers.add("CONTRADICTORY_FACTS")
        if len(class_values) > 1:
            blockers.add("AMBIGUOUS_CLASS_COVERAGE")
        shares_count: int | None = None
        if (
            not invalid_fact_rows
            and not unsupported_form
            and not invalid_count
            and not invalid_required_dates
            and len(count_values) == 1
            and len(class_values) <= 1
        ):
            shares_count = next(iter(count_values))

        observations.append({
            "accession": accession_value,
            "accepted_at": _timestamp_text(accepted),
            "report_date": report_date.isoformat(),
            "fetched_at": _timestamp_text(fetched),
            "filing_class": filing_class,
            "shares_count": shares_count,
        })

    if matching_records == 0:
        blockers.add("SHARES_FACT_ABSENT")
    if blockers & _COUNT_SUPPRESSING_BLOCKERS:
        for observation in observations:
            observation["shares_count"] = None
    result["blockers"] = sorted(blockers)
    return result
