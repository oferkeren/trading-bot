"""Run existing micro-cap research CLIs over a batch manifest; always NO_TRADE."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from microcap_batch_schema import (
    BIAS, REQUIRED_BLOCKERS, STAGE_STATUSES, STAGES, coverage_from_rows,
    validate_batch_report, validate_blockers, validate_manifest, validate_row,
)
from microcap_history import CoverageError
from microcap_readiness import (
    _external, _load, _repository_roots, _save, _validate_sec, project_readiness,
)


_REPOSITORY = Path(__file__).resolve().parent
STAGE_TIMEOUT = 300
_MAX_STDOUT = 2 * 1024 * 1024
_SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_NEWS_OBSERVED = ("ARTICLES_OBSERVED", "EMPTY")

StageRunner = Callable[[str, list[str]], tuple[int, bytes]]


def run_stage(script: str, args: list[str]) -> tuple[int, bytes]:
    completed = subprocess.run(
        [sys.executable, str(_REPOSITORY / script), *args], cwd=_REPOSITORY,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        timeout=STAGE_TIMEOUT, check=False,
    )
    return completed.returncode, completed.stdout


def _parse_object(payload: bytes) -> dict | None:
    if len(payload) > _MAX_STDOUT:
        return None
    try:
        value = json.loads(payload)
    except (UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _write_inputs(sample: Mapping, directory: Path) -> tuple[Path, Path, Path]:
    roster = {
        "source_url": _SEC_TICKERS_URL, "retrieved_at": sample["start_utc"],
        "coverage_claim": "pilot-unverified",
        "members": [{
            "issuer_id": sample["issuer_id"], "symbol": sample["symbol"],
            "company": sample["company"], "valid_from": f"{sample['date']}T00:00:00Z",
            "valid_to": None, "listing_status": "listed",
            "source_record_id": f"sec-cik-{sample['issuer_id']}-{sample['symbol'].lower()}",
        }],
    }
    ibkr_manifest = {"status": "predeclared", "issuer_ids": [sample["issuer_id"]],
                     "dates": [sample["date"]]}
    paths = (directory / "roster.json", directory / "ibkr-manifest.json",
             directory / "source-manifest.json")
    _save(paths[0], roster)
    _save(paths[1], ibkr_manifest)
    _save(paths[2], {**ibkr_manifest, "symbol": sample["symbol"]})
    return paths


def _run_ibkr(runner: StageRunner, sample: Mapping, directory: Path,
              roster: Path, manifest: Path) -> tuple[str, dict | None]:
    try:
        code, stdout = runner("microcap_coverage_probe.py", [
            "--roster", str(roster), "--sample-manifest", str(manifest),
            "--start", sample["start_utc"], "--end", sample["end_utc"],
            "--pilot", "--fetch", "--max-symbols", "1", "--max-days", "1"])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code not in (0, 3):
        return "PROVIDER_ERROR", None
    report = _parse_object(stdout)
    if report is None:
        return "INVALID", None
    _save(directory / "ibkr-report.json", report)
    return ("OK" if code == 0 else "PROVIDER_ERROR"), report


def _run_source(runner: StageRunner, sample: Mapping, directory: Path,
                ibkr: Mapping, manifest: Path) -> tuple[str, dict | None]:
    base = json.loads(json.dumps(ibkr))
    if not isinstance(base.get("sample_manifest"), dict):
        return "INVALID", None
    base["sample_manifest"]["symbol"] = sample["symbol"]
    _save(directory / "base-report.json", base)
    try:
        code, stdout = runner("microcap_source_probe.py", [
            "--base-report", str(directory / "base-report.json"),
            "--sample-manifest", str(manifest),
            "--start", sample["start_utc"], "--end", sample["end_utc"],
            "--fetch", "--max-pages", "2"])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code == 3:
        return "PROVIDER_ERROR", None
    report = _parse_object(stdout) if code == 0 else None
    if report is None:
        return "INVALID", None
    _save(directory / "source-report.json", report)
    return "OK", report


def _run_sec(runner: StageRunner, sample: Mapping, directory: Path) -> tuple[str, dict | None]:
    output = directory / "sec-report.json"
    output.unlink(missing_ok=True)
    try:
        code, _ = runner("microcap_sec_probe.py", [
            "--cik", sample["issuer_id"], "--decision-at", sample["start_utc"],
            "--fetch", "--output", str(output)])
    except subprocess.TimeoutExpired:
        return "TIMEOUT", None
    if code == 3:
        return "PROVIDER_ERROR", None
    if code != 0 or not output.is_file():
        return "INVALID", None
    try:
        return "OK", _load(output)
    except CoverageError:
        return "INVALID", None


def summarize(sample: Mapping, stages: dict[str, str], source: Mapping | None,
              sec: Mapping | None, *, now: datetime) -> tuple[dict, list[str]]:
    projection = None
    if source is not None:
        for candidate in (sec, None) if sec is not None else (None,):
            try:
                projection = project_readiness(source, candidate, now=now)
            except CoverageError:
                continue
            if candidate is None and sec is not None:
                stages["sec"] = "INVALID"
            break
        expected = ({"issuer_id": sample["issuer_id"], "symbol": sample["symbol"],
                     "date": sample["date"]},
                    {"start_utc": sample["start_utc"], "end_utc": sample["end_utc"]})
        if projection is None or (projection["sample"], projection["sample_window"]) != expected:
            projection = None
            stages["source"] = "INVALID"
    if projection is not None:
        sources = projection["sources"]
        news = sources["news"]
        row_values = {
            "ibkr_minute": sources["ibkr"]["minute_cells"] > 0,
            "ibkr_quotes": sources["ibkr"]["quote_cells"] > 0,
            "roster": sources["roster"]["evidence_status"],
            "news_count": (news["article_count"]
                           if news["status"] in _NEWS_OBSERVED
                           and type(news["article_count"]) is int else None),
            "sec_observations": sources["sec"]["observation_count"],
        }
        blockers = set(projection["blockers"])
    else:
        sec_count, sec_blockers = 0, []
        if sec is not None:
            try:
                sec_count, sec_blockers = _validate_sec(
                    sec, issuer=sample["issuer_id"], start=sample["start_utc"],
                    end=sample["end_utc"])
            except CoverageError:
                stages["sec"] = "INVALID"
        row_values = {"ibkr_minute": False, "ibkr_quotes": False, "roster": "MISSING",
                      "news_count": None, "sec_observations": sec_count}
        blockers = set(sec_blockers) | {"SAMPLE_INCOMPLETE"}
    errors = sorted(f"{stage}:{stages[stage]}" for stage in STAGES if stages[stage] != "OK")
    if errors:
        blockers.add("SAMPLE_INCOMPLETE")
    row = {"date": sample["date"], "symbol": sample["symbol"],
           "issuer_id": sample["issuer_id"], "move_pct": sample["move_pct"],
           **row_values, "stage_errors": errors}
    validate_row(row)
    return row, sorted(blockers | REQUIRED_BLOCKERS)


def _result(sample, stages, row, blockers, manifest_sha256) -> dict:
    return {"kind": "microcap_batch_sample_result", "schema_version": 1,
            "manifest_sha256": manifest_sha256, "stages": stages, "row": row,
            "blockers": blockers}


def run_sample(sample: Mapping, directory: Path, *, runner: StageRunner,
               manifest_sha256: str, now: datetime) -> dict:
    directory.mkdir(mode=0o700, exist_ok=True)
    (directory / "sample-result.json").unlink(missing_ok=True)
    roster, ibkr_manifest, source_manifest = _write_inputs(sample, directory)
    stages = {"ibkr": "SKIPPED", "source": "SKIPPED", "sec": "SKIPPED"}
    stages["ibkr"], ibkr = _run_ibkr(runner, sample, directory, roster, ibkr_manifest)
    source = None
    if ibkr is not None:
        stages["source"], source = _run_source(runner, sample, directory, ibkr,
                                               source_manifest)
    stages["sec"], sec = _run_sec(runner, sample, directory)
    row, blockers = summarize(sample, stages, source, sec, now=now)
    result = _result(sample, stages, row, blockers, manifest_sha256)
    _save(directory / "sample-result.json", result)
    return result


def load_sample_result(directory: Path, sample: Mapping, manifest_sha256: str) -> dict | None:
    path = directory / "sample-result.json"
    if not path.is_file():
        return None
    try:
        value = _load(path)
        stages, row = value.get("stages"), value.get("row")
        if (set(value) != {"kind", "schema_version", "manifest_sha256", "stages", "row",
                           "blockers"}
                or value["kind"] != "microcap_batch_sample_result"
                or value["schema_version"] != 1
                or value["manifest_sha256"] != manifest_sha256
                or not isinstance(stages, dict) or set(stages) != set(STAGES)
                or any(status not in STAGE_STATUSES for status in stages.values())):
            return None
        validate_row(row)
        validate_blockers(value["blockers"])
        expected_errors = sorted(f"{s}:{stages[s]}" for s in STAGES if stages[s] != "OK")
        if (row["stage_errors"] != expected_errors
                or (row["date"], row["symbol"], row["issuer_id"], row["move_pct"])
                != (sample["date"], sample["symbol"], sample["issuer_id"],
                    sample["move_pct"])):
            return None
        if row["sec_observations"] and stages["sec"] != "OK":
            return None
    except CoverageError:
        return None
    return value


_REQUIRED_ENV = ("IB_PROBE_CLIENT_ID", "MASSIVE_API_KEY", "APCA_API_KEY_ID",
                 "APCA_API_SECRET_KEY", "SEC_USER_AGENT")
SAMPLE_SPACING = 25.0
_sleep = time.sleep


def _progress(index: int, total: int, row: Mapping, resumed: bool) -> None:
    print(json.dumps({"sample": index, "of": total, "symbol": row["symbol"],
                      "date": row["date"], "stage_errors": row["stage_errors"],
                      "resumed": resumed}, separators=(",", ":")),
          file=sys.stderr, flush=True)


def run_batch(manifest: Mapping, out_dir: Path, *, runner: StageRunner, resume: bool,
              now_fn: Callable[[], datetime], clock: Callable[[], float] = time.monotonic,
              sleep: Callable[[float], None] = time.sleep,
              spacing: float = SAMPLE_SPACING, progress: bool = False) -> dict:
    validate_manifest(manifest)
    samples = manifest["samples"]
    rows, blockers, last_start = [], set(), None
    for index, sample in enumerate(samples, start=1):
        directory = out_dir / f"{sample['date']}-{sample['issuer_id']}"
        result = (load_sample_result(directory, sample, manifest["sha256"])
                  if resume else None)
        resumed = result is not None
        if result is None:
            if last_start is not None and clock() - last_start < spacing:
                sleep(spacing - (clock() - last_start))
            last_start = clock()
            result = run_sample(sample, directory, runner=runner,
                                manifest_sha256=manifest["sha256"], now=now_fn())
        rows.append(result["row"])
        blockers |= set(result["blockers"])
        if progress:
            _progress(index, len(samples), result["row"], resumed)
    report = {
        "kind": "microcap_batch_report", "schema_version": 1,
        "generated_at": now_fn().astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "decision": "NO_TRADE", "order_approval": False, "model_calibrated": False,
        "target_probabilities": "unavailable",
        "batch": {"as_of": manifest["as_of"], "manifest_sha256": manifest["sha256"],
                  "sample_count": len(rows), "rule": manifest["rule"], "bias": list(BIAS)},
        "coverage": coverage_from_rows(rows), "samples": rows,
        "blockers": sorted(blockers),
    }
    validate_batch_report(report)
    _save(out_dir / "batch-report.json", report)
    return report


def _external_dir(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise CoverageError("INPUT_INVALID")
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError):
        raise CoverageError("INPUT_INVALID") from None
    if (not resolved.is_dir()
            or any(resolved.is_relative_to(root) for root in _repository_roots())):
        raise CoverageError("INPUT_INVALID")
    return resolved


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CoverageError("INPUT_INVALID")


def _fail(code: str) -> int:
    print(json.dumps({"error": code}), file=sys.stderr)
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Run the micro-cap coverage pilot over a batch manifest; "
                                 "always NO_TRADE")
    parser.add_argument("--manifest", required=True, help="absolute external batch manifest")
    parser.add_argument("--out-dir", required=True, help="absolute external output directory")
    parser.add_argument("--resume", action="store_true",
                        help="reuse valid sample-result.json files")
    try:
        args = parser.parse_args(argv)
        if any(not os.environ.get(name, "").strip() for name in _REQUIRED_ENV):
            return _fail("ENVIRONMENT_INCOMPLETE")
        manifest = _load(_external(Path(args.manifest), existing=True))
        out_dir = _external_dir(args.out_dir)
        report = run_batch(manifest, out_dir, runner=run_stage, resume=args.resume,
                           now_fn=lambda: datetime.now(timezone.utc), sleep=_sleep,
                           progress=True)
    except CoverageError:
        return _fail("INPUT_INVALID")
    print(json.dumps({"batch_report": str(out_dir / "batch-report.json"),
                      "coverage": report["coverage"], "samples": len(report["samples"])},
                     sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
