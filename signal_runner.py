import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


# ============================================================
# CONFIG
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


BRIDGE_FILE = (
    BASE_DIR
    /
    "signal_bridge.py"
)


PYTHON_BIN = (
    BASE_DIR
    /
    "venv"
    /
    "bin"
    /
    "python"
)


LOOP_SECONDS = int(
    os.getenv(
        "TRADINGMAX_SCAN_INTERVAL_SECONDS",
        "60",
    )
)


FAILURE_SLEEP_SECONDS = int(
    os.getenv(
        "TRADINGMAX_FAILURE_SLEEP_SECONDS",
        "30",
    )
)


RUN_ONCE = (
    str(
        os.getenv(
            "TRADINGMAX_RUN_ONCE",
            "false",
        )
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


# ============================================================
# TIME
# ============================================================

def utc_now():
    return (
        datetime.now(
            timezone.utc
        )
    )


def utc_now_text():
    return (
        utc_now()
        .isoformat()
    )


# ============================================================
# VALIDATION
# ============================================================

def validate_environment():
    if not PYTHON_BIN.exists():

        raise RuntimeError(
            f"Python executable not found: "
            f"{PYTHON_BIN}"
        )

    if not BRIDGE_FILE.exists():

        raise RuntimeError(
            f"Bridge file not found: "
            f"{BRIDGE_FILE}"
        )

    if LOOP_SECONDS < 15:

        raise RuntimeError(
            "TRADINGMAX_SCAN_INTERVAL_SECONDS "
            "must be >= 15"
        )


# ============================================================
# RUN BRIDGE
# ============================================================

def run_bridge():
    started = time.monotonic()

    print()
    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX AUTONOMOUS CYCLE"
    )

    print(
        "=============================================================="
    )

    print(
        f"Started UTC : "
        f"{utc_now_text()}"
    )

    print(
        f"Interval    : "
        f"{LOOP_SECONDS}s"
    )

    print(
        f"Python      : "
        f"{PYTHON_BIN}"
    )

    print(
        f"Bridge      : "
        f"{BRIDGE_FILE}"
    )

    print(
        "=============================================================="
    )

    environment = dict(
        os.environ
    )

    process = subprocess.run(
        [
            str(
                PYTHON_BIN
            ),
            str(
                BRIDGE_FILE
            ),
        ],
        cwd=str(
            BASE_DIR
        ),
        env=environment,
        check=False,
    )

    elapsed = (
        time.monotonic()
        -
        started
    )

    print()
    print(
        "CYCLE COMPLETE | "
        f"return_code="
        f"{process.returncode} | "
        f"elapsed="
        f"{elapsed:.1f}s"
    )

    return (
        process.returncode,
        elapsed,
    )


# ============================================================
# LOOP
# ============================================================

def main():
    validate_environment()

    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX AUTONOMOUS SIGNAL RUNNER"
    )

    print(
        "=============================================================="
    )

    print(
        f"Started UTC  : "
        f"{utc_now_text()}"
    )

    print(
        f"Loop seconds : "
        f"{LOOP_SECONDS}"
    )

    print(
        f"Run once     : "
        f"{RUN_ONCE}"
    )

    print(
        "=============================================================="
    )

    cycle_number = 0

    while True:

        cycle_number += 1

        print()
        print(
            "##############################################################"
        )

        print(
            f"CYCLE #{cycle_number}"
        )

        print(
            "##############################################################"
        )

        try:

            return_code, elapsed = (
                run_bridge()
            )

        except KeyboardInterrupt:

            raise

        except Exception as exc:

            print(
                "RUNNER ERROR | "
                f"{type(exc).__name__}: "
                f"{exc}"
            )

            return_code = 1

            elapsed = 0.0

        if RUN_ONCE:

            print(
                "RUN_ONCE enabled. "
                "Stopping runner."
            )

            return (
                0
                if return_code == 0
                else
                1
            )

        if return_code == 0:

            sleep_seconds = max(
                1,
                LOOP_SECONDS
                -
                int(
                    elapsed
                ),
            )

        else:

            sleep_seconds = (
                FAILURE_SLEEP_SECONDS
            )

        print(
            f"NEXT CYCLE | "
            f"in {sleep_seconds}s"
        )

        try:

            time.sleep(
                sleep_seconds
            )

        except KeyboardInterrupt:

            raise


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    try:

        exit_code = (
            main()
        )

        sys.exit(
            exit_code
            if exit_code
            is not None
            else
            0
        )

    except KeyboardInterrupt:

        print()
        print(
            "RUNNER STOPPED BY USER"
        )

        sys.exit(
            130
        )

    except Exception as exc:

        print(
            "RUNNER FAILED | "
            f"{type(exc).__name__}: "
            f"{exc}"
        )

        sys.exit(
            1
        )
