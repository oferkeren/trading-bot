"""Offline-first, research-only SEC filing and shares feasibility probe."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from microcap_history import CoverageError
from microcap_sec_evidence import observe_shares
from microcap_sec_reader import SecReader


_REPOSITORY = Path(__file__).resolve().parent
_CIK = re.compile(r"[0-9]{1,10}\Z")
_UTC = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z\Z")
_MAX_SAVED_BYTES = 2 * 1024 * 1024
_PROVIDER_ERRORS = frozenset({
    "SEC_ACCESS_UNAVAILABLE", "SEC_RATE_LIMITED", "SEC_RESPONSE_INVALID",
})


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageError("INPUT_INVALID")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(description="Read-only SEC shares feasibility; always NO_TRADE")
    parser.add_argument("--cik", required=True, help="issuer CIK, 1–10 ASCII digits")
    parser.add_argument("--decision-at", required=True, help="decision instant in UTC (YYYY-MM-DDTHH:MM:SSZ)")
    parser.add_argument("--submissions", help="absolute external saved SEC submissions JSON (offline only)")
    parser.add_argument("--companyfacts", help="absolute external saved SEC companyfacts JSON (offline only)")
    parser.add_argument("--fetch", action="store_true", help="opt in to one SEC submissions and one companyfacts GET")
    parser.add_argument("--output", help="absolute external JSON report path (required with --fetch)")
    return parser


def _external(value: str, *, existing: bool) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CoverageError("INPUT_INVALID")
    try:
        resolved = path.resolve()
    except (OSError, RuntimeError):
        raise CoverageError("INPUT_INVALID") from None
    if resolved.is_relative_to(_REPOSITORY):
        raise CoverageError("INPUT_INVALID")
    if existing:
        if not resolved.is_file():
            raise CoverageError("INPUT_INVALID")
    elif not resolved.parent.is_dir() or resolved.is_dir():
        raise CoverageError("INPUT_INVALID")
    return resolved


def _validate(args: argparse.Namespace) -> tuple[str, Path | None, Path | None, Path | None]:
    if not _CIK.fullmatch(args.cik) or not _UTC.fullmatch(args.decision_at):
        raise CoverageError("INPUT_INVALID")
    try:
        datetime.fromisoformat(args.decision_at.replace("Z", "+00:00"))
    except ValueError:
        raise CoverageError("INPUT_INVALID") from None
    if args.fetch:
        if args.submissions or args.companyfacts or not args.output:
            raise CoverageError("INPUT_INVALID")
    elif not args.submissions or not args.companyfacts:
        raise CoverageError("INPUT_INVALID")
    submissions = _external(args.submissions, existing=True) if args.submissions else None
    facts = _external(args.companyfacts, existing=True) if args.companyfacts else None
    output = _external(args.output, existing=False) if args.output else None
    if (submissions is not None and submissions == facts
            or output is not None and output in (submissions, facts)):
        raise CoverageError("INPUT_INVALID")
    return args.cik.zfill(10), submissions, facts, output


def _reject_constant(value: str) -> None:
    raise ValueError("invalid JSON constant")


def _load(path: Path) -> dict:
    try:
        with path.open("rb") as source:
            payload = source.read(_MAX_SAVED_BYTES + 1)
        if len(payload) > _MAX_SAVED_BYTES:
            raise CoverageError("INPUT_INVALID")
        document = json.loads(payload, parse_constant=_reject_constant)
    except (OSError, ValueError, UnicodeDecodeError, RecursionError):
        raise CoverageError("INPUT_INVALID") from None
    if not isinstance(document, dict):
        raise CoverageError("INPUT_INVALID")
    return document


def _save(path: Path, payload: str) -> None:
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, prefix=".sec-probe-",
            suffix=".json", delete=False,
        ) as destination:
            temporary = destination.name
            destination.write(payload + "\n")
        os.replace(temporary, path)
    except OSError:
        raise CoverageError("INPUT_INVALID") from None
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def _private(value: str) -> bool:
    user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
    return bool(user_agent and user_agent in value)


def _error(status: int, reason: str) -> int:
    alternatives = (
        ({"error": reason}, {"failure": "INVALID_INPUT"}, {"fail": 2})
        if status == 2 else
        ({"error": reason}, {"failure": "PROVIDER_ERROR"}, {"fail": 3})
    )
    for value in alternatives:
        text = json.dumps(value, separators=(",", ":"))
        if not _private(text):
            print(text, file=sys.stderr)
            break
    return status


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        cik, submissions_path, facts_path, output = _validate(args)
        if not args.fetch:
            submissions = _load(submissions_path)
            facts = _load(facts_path)
    except (CoverageError, OSError, ValueError, TypeError):
        return _error(2, "INPUT_INVALID")

    if args.fetch:
        try:
            submissions, facts = SecReader.from_environment().fetch(cik)
        except CoverageError as error:
            reason = str(error)
            if reason == "SEC_USER_AGENT_MISSING":
                return _error(2, reason)
            return _error(3, reason if reason in _PROVIDER_ERRORS else "SEC_ACCESS_UNAVAILABLE")
        except (OSError, TimeoutError):
            return _error(3, "SEC_ACCESS_UNAVAILABLE")

    fetched_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    evidence = observe_shares(
        submissions, facts, cik=cik, decision_at=args.decision_at, fetched_at=fetched_at,
    )
    report = {
        "decision": "NO_TRADE",
        "order_approval": False,
        "model_calibrated": False,
        "target_probabilities": "unavailable",
        "market_cap_gate": evidence,
    }
    payload = json.dumps(report, sort_keys=True)
    if _private(payload):
        return _error(2, "INPUT_INVALID")
    try:
        if output is not None:
            _save(output, payload)
    except CoverageError:
        return _error(2, "INPUT_INVALID")
    print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
