import json
import os
import sqlite3
import subprocess
import time

from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


PYTHON_BIN = (
    BASE_DIR
    /
    "venv"
    /
    "bin"
    /
    "python"
)


DB_FILE = (
    BASE_DIR
    /
    "early_momentum.db"
)


REPORT_DIR = (
    BASE_DIR
    /
    "research_snapshots"
)


LATEST_REPORT = (
    REPORT_DIR
    /
    "latest.txt"
)


LATEST_JSON = (
    REPORT_DIR
    /
    "latest.json"
)


# ============================================================
# CONFIG
# ============================================================

RETENTION_DAYS = 90


SCRIPT_TIMEOUT_SECONDS = 180


RESEARCH_SCRIPTS = [
    (
        "V2 FORWARD ROBUSTNESS",
        BASE_DIR
        /
        "v2_forward_robustness.py",
    ),

    (
        "MICRO READY RESEARCH",
        BASE_DIR
        /
        "forward_micro_ready_research.py",
    ),

    (
        "FORWARD MODEL RESEARCH",
        BASE_DIR
        /
        "forward_model_research.py",
    ),
]


# ============================================================
# HELPERS
# ============================================================

def local_now():
    return datetime.now().astimezone()


def utc_now():
    return datetime.now(
        timezone.utc
    )


def db_connect():
    conn = sqlite3.connect(
        DB_FILE,
        timeout=10,
    )

    conn.row_factory = sqlite3.Row

    return conn


def table_exists(
    conn,
    table_name,
):
    row = conn.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE
            type = 'table'
            AND name = ?
        LIMIT 1
        """,
        (
            table_name,
        ),
    ).fetchone()

    return (
        row is not None
    )


def safe_unlink(
    path,
):
    try:
        if (
            path.exists()
            or
            path.is_symlink()
        ):
            path.unlink()

    except Exception:
        pass


# ============================================================
# DATABASE SUMMARY
# ============================================================

def collect_database_summary():
    result = {
        "database":
            str(
                DB_FILE
            ),

        "database_exists":
            DB_FILE.exists(),

        "forward_meta":
            None,

        "outcome_rows":
            0,

        "unique_snapshots":
            0,

        "first_snapshot_ts":
            None,

        "last_snapshot_ts":
            None,

        "by_horizon_action":
            [],
    }


    if not DB_FILE.exists():
        return result


    conn = db_connect()


    try:
        if table_exists(
            conn,
            "early_v2_forward_meta",
        ):
            row = conn.execute(
                """
                SELECT
                    forward_start_ts,
                    tracker_version,
                    created_at

                FROM early_v2_forward_meta

                WHERE id = 1
                """
            ).fetchone()


            if row is not None:
                result[
                    "forward_meta"
                ] = {
                    "forward_start_ts":
                        row[
                            "forward_start_ts"
                        ],

                    "tracker_version":
                        row[
                            "tracker_version"
                        ],

                    "created_at":
                        row[
                            "created_at"
                        ],
                }


        if table_exists(
            conn,
            "early_v2_outcomes",
        ):
            row = conn.execute(
                """
                SELECT
                    COUNT(*)
                        AS outcome_rows,

                    COUNT(
                        DISTINCT snapshot_id
                    )
                        AS unique_snapshots,

                    MIN(
                        snapshot_ts
                    )
                        AS first_snapshot_ts,

                    MAX(
                        snapshot_ts
                    )
                        AS last_snapshot_ts

                FROM early_v2_outcomes
                """
            ).fetchone()


            result[
                "outcome_rows"
            ] = int(
                row[
                    "outcome_rows"
                ]
                or
                0
            )


            result[
                "unique_snapshots"
            ] = int(
                row[
                    "unique_snapshots"
                ]
                or
                0
            )


            result[
                "first_snapshot_ts"
            ] = row[
                "first_snapshot_ts"
            ]


            result[
                "last_snapshot_ts"
            ] = row[
                "last_snapshot_ts"
            ]


            rows = conn.execute(
                """
                SELECT
                    horizon_seconds,
                    action,
                    COUNT(*)
                        AS samples

                FROM early_v2_outcomes

                GROUP BY
                    horizon_seconds,
                    action

                ORDER BY
                    horizon_seconds,
                    action
                """
            ).fetchall()


            result[
                "by_horizon_action"
            ] = [
                {
                    "horizon_seconds":
                        int(
                            row[
                                "horizon_seconds"
                            ]
                        ),

                    "action":
                        row[
                            "action"
                        ],

                    "samples":
                        int(
                            row[
                                "samples"
                            ]
                        ),
                }

                for row
                in rows
            ]


    finally:
        conn.close()


    return result


# ============================================================
# SCRIPT EXECUTION
# ============================================================

def run_research_script(
    title,
    script_path,
):
    result = {
        "title":
            title,

        "script":
            str(
                script_path
            ),

        "exists":
            script_path.exists(),

        "return_code":
            None,

        "duration_seconds":
            None,

        "stdout":
            "",

        "stderr":
            "",
    }


    if not script_path.exists():
        result[
            "stderr"
        ] = (
            "Script not found: "
            f"{script_path}"
        )

        return result


    started = time.monotonic()


    try:
        completed = subprocess.run(
            [
                str(
                    PYTHON_BIN
                ),
                str(
                    script_path
                ),
            ],
            cwd=
                str(
                    BASE_DIR
                ),
            capture_output=True,
            text=True,
            timeout=
                SCRIPT_TIMEOUT_SECONDS,
            check=False,
        )


        result[
            "return_code"
        ] = completed.returncode


        result[
            "stdout"
        ] = (
            completed.stdout
            or
            ""
        )


        result[
            "stderr"
        ] = (
            completed.stderr
            or
            ""
        )


    except subprocess.TimeoutExpired as exc:
        result[
            "return_code"
        ] = -1


        result[
            "stdout"
        ] = (
            exc.stdout
            or
            ""
        )


        result[
            "stderr"
        ] = (
            "TIMEOUT after "
            f"{SCRIPT_TIMEOUT_SECONDS}s"
        )


    except Exception as exc:
        result[
            "return_code"
        ] = -1


        result[
            "stderr"
        ] = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )


    result[
        "duration_seconds"
    ] = (
        time.monotonic()
        -
        started
    )


    return result


# ============================================================
# REPORT
# ============================================================

def build_text_report(
    generated_local,
    generated_utc,
    db_summary,
    script_results,
):
    lines = []


    lines.append(
        "=============================================================="
    )

    lines.append(
        "TRADINGMAX DAILY RESEARCH SNAPSHOT"
    )

    lines.append(
        "=============================================================="
    )


    lines.append(
        f"Generated local : "
        f"{generated_local.isoformat()}"
    )


    lines.append(
        f"Generated UTC   : "
        f"{generated_utc.isoformat()}"
    )


    lines.append(
        f"Database        : "
        f"{DB_FILE}"
    )


    lines.append(
        ""
    )


    lines.append(
        "=============================================================="
    )

    lines.append(
        "FORWARD DATA STATUS"
    )

    lines.append(
        "=============================================================="
    )


    meta = db_summary.get(
        "forward_meta"
    )


    if meta is None:
        lines.append(
            "Forward meta     : MISSING"
        )

    else:
        lines.append(
            f"Forward start ts : "
            f"{meta.get('forward_start_ts')}"
        )


        lines.append(
            f"Tracker version  : "
            f"{meta.get('tracker_version')}"
        )


    lines.append(
        f"Outcome rows     : "
        f"{db_summary.get('outcome_rows', 0)}"
    )


    lines.append(
        f"Unique snapshots : "
        f"{db_summary.get('unique_snapshots', 0)}"
    )


    lines.append(
        f"First snapshot   : "
        f"{db_summary.get('first_snapshot_ts')}"
    )


    lines.append(
        f"Last snapshot    : "
        f"{db_summary.get('last_snapshot_ts')}"
    )


    lines.append(
        ""
    )


    lines.append(
        "BY HORIZON / ACTION"
    )


    for row in db_summary.get(
        "by_horizon_action",
        [],
    ):
        lines.append(
            f"{row['horizon_seconds']:>3}s | "
            f"{row['action']:<4} | "
            f"n={row['samples']}"
        )


    for result in script_results:
        lines.append(
            ""
        )


        lines.append(
            "=============================================================="
        )

        lines.append(
            result[
                "title"
            ]
        )

        lines.append(
            "=============================================================="
        )


        lines.append(
            f"Script       : "
            f"{result['script']}"
        )


        lines.append(
            f"Exists       : "
            f"{result['exists']}"
        )


        lines.append(
            f"Return code  : "
            f"{result['return_code']}"
        )


        lines.append(
            f"Duration     : "
            f"{result['duration_seconds']}"
        )


        lines.append(
            ""
        )


        stdout = (
            result.get(
                "stdout"
            )
            or
            ""
        ).rstrip()


        stderr = (
            result.get(
                "stderr"
            )
            or
            ""
        ).rstrip()


        if stdout:
            lines.append(
                stdout
            )


        if stderr:
            lines.append(
                ""
            )

            lines.append(
                "---------------- STDERR ----------------"
            )

            lines.append(
                stderr
            )


    lines.append(
        ""
    )


    lines.append(
        "=============================================================="
    )

    lines.append(
        "END OF DAILY RESEARCH SNAPSHOT"
    )

    lines.append(
        "=============================================================="
    )


    return (
        "\n".join(
            lines
        )
        +
        "\n"
    )


# ============================================================
# STORAGE
# ============================================================

def write_snapshot(
    generated_local,
    generated_utc,
    db_summary,
    script_results,
):
    REPORT_DIR.mkdir(
        parents=True,
        exist_ok=True,
    )


    stamp = generated_local.strftime(
        "%Y%m%d-%H%M%S"
    )


    report_file = (
        REPORT_DIR
        /
        f"research-{stamp}.txt"
    )


    json_file = (
        REPORT_DIR
        /
        f"research-{stamp}.json"
    )


    text_report = (
        build_text_report(
            generated_local=
                generated_local,
            generated_utc=
                generated_utc,
            db_summary=
                db_summary,
            script_results=
                script_results,
        )
    )


    report_file.write_text(
        text_report,
        encoding="utf-8",
    )


    json_payload = {
        "generated_local":
            generated_local.isoformat(),

        "generated_utc":
            generated_utc.isoformat(),

        "database_summary":
            db_summary,

        "research_scripts":
            [
                {
                    "title":
                        result[
                            "title"
                        ],

                    "script":
                        result[
                            "script"
                        ],

                    "exists":
                        result[
                            "exists"
                        ],

                    "return_code":
                        result[
                            "return_code"
                        ],

                    "duration_seconds":
                        result[
                            "duration_seconds"
                        ],
                }

                for result
                in script_results
            ],
    }


    json_file.write_text(
        json.dumps(
            json_payload,
            indent=2,
            sort_keys=True,
        )
        +
        "\n",
        encoding="utf-8",
    )


    safe_unlink(
        LATEST_REPORT
    )


    safe_unlink(
        LATEST_JSON
    )


    LATEST_REPORT.symlink_to(
        report_file.name
    )


    LATEST_JSON.symlink_to(
        json_file.name
    )


    return (
        report_file,
        json_file,
    )


# ============================================================
# RETENTION
# ============================================================

def cleanup_old_reports():
    if not REPORT_DIR.exists():
        return 0


    cutoff = (
        time.time()
        -
        (
            RETENTION_DAYS
            *
            86400
        )
    )


    removed = 0


    for path in REPORT_DIR.iterdir():
        if path.is_symlink():
            continue


        if not path.is_file():
            continue


        if not (
            path.name.startswith(
                "research-"
            )
            and
            path.suffix
            in {
                ".txt",
                ".json",
            }
        ):
            continue


        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()

                removed += 1

        except FileNotFoundError:
            pass


    return removed


# ============================================================
# MAIN
# ============================================================

def main():
    generated_local = local_now()

    generated_utc = utc_now()


    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX DAILY RESEARCH SNAPSHOT"
    )

    print(
        "=============================================================="
    )


    print(
        f"Started local : "
        f"{generated_local.isoformat()}"
    )


    db_summary = (
        collect_database_summary()
    )


    print(
        f"Outcome rows  : "
        f"{db_summary.get('outcome_rows', 0)}"
    )


    print(
        f"Snapshots     : "
        f"{db_summary.get('unique_snapshots', 0)}"
    )


    script_results = []


    for (
        title,
        script_path,
    ) in RESEARCH_SCRIPTS:

        print(
            f"RUN | {title}",
            flush=True,
        )


        result = (
            run_research_script(
                title,
                script_path,
            )
        )


        script_results.append(
            result
        )


        print(
            f"DONE | "
            f"{title} | "
            f"rc="
            f"{result['return_code']} | "
            f"time="
            f"{result['duration_seconds']:.2f}s",
            flush=True,
        )


    (
        report_file,
        json_file,
    ) = write_snapshot(
        generated_local=
            generated_local,
        generated_utc=
            generated_utc,
        db_summary=
            db_summary,
        script_results=
            script_results,
    )


    removed = (
        cleanup_old_reports()
    )


    failures = [
        result
        for result
        in script_results
        if result[
            "return_code"
        ]
        !=
        0
    ]


    print()
    print(
        f"REPORT   | {report_file}"
    )


    print(
        f"JSON     | {json_file}"
    )


    print(
        f"LATEST   | {LATEST_REPORT}"
    )


    print(
        f"RETENTION| removed={removed}"
    )


    if failures:
        print(
            f"STATUS   | PARTIAL FAILURE | "
            f"failed={len(failures)}"
        )


        raise SystemExit(
            1
        )


    print(
        "STATUS   | SUCCESS"
    )


if __name__ == "__main__":
    main()
