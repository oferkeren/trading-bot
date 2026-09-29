import os
import signal
import subprocess
import time

from datetime import (
    datetime,
    timezone,
)

from pathlib import Path

from dotenv import load_dotenv


# ============================================================
# PATHS
# ============================================================

BASE_DIR = Path(
    __file__
).resolve().parent


ENV_FILE = (
    BASE_DIR
    /
    ".env"
)


BRIDGE_FILE = (
    BASE_DIR
    /
    "ai_signal_bridge.py"
)


OUTCOME_TRACKER_FILE = (
    BASE_DIR
    /
    "early_outcome_tracker.py"
)


V2_SHADOW_FILE = (
    BASE_DIR
    /
    "early_v2_shadow.py"
)


V2_OUTCOME_TRACKER_FILE = (
    BASE_DIR
    /
    "early_v2_outcome_tracker.py"
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


load_dotenv(
    ENV_FILE
)


# ============================================================
# CONFIG
# ============================================================

SCAN_INTERVAL_SECONDS = int(
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
    os.getenv(
        "TRADINGMAX_RUN_ONCE",
        "false",
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


EARLY_OUTCOME_TRACKER_ENABLED = (
    os.getenv(
        "EARLY_OUTCOME_TRACKER_ENABLED",
        "true",
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


EARLY_OUTCOME_TRACKER_FAIL_CLOSED = (
    os.getenv(
        "EARLY_OUTCOME_TRACKER_FAIL_CLOSED",
        "false",
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


EARLY_V2_SHADOW_ENABLED = (
    os.getenv(
        "EARLY_V2_SHADOW_ENABLED",
        "true",
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


EARLY_V2_OUTCOME_ENABLED = (
    os.getenv(
        "EARLY_V2_OUTCOME_ENABLED",
        "true",
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
# STATE
# ============================================================

_stop_requested = False

_current_process = None


# ============================================================
# HELPERS
# ============================================================

def utc_now():
    return datetime.now(
        timezone.utc
    )


def request_stop(
    signum,
    frame,
):
    global _stop_requested


    _stop_requested = True


    print(
        "RUNNER STOP REQUESTED | "
        f"signal={signum}",
        flush=True,
    )


    process = _current_process


    if (
        process is not None
        and
        process.poll()
        is None
    ):
        try:
            process.terminate()

        except Exception:
            pass


def validate_environment():
    required = [
        PYTHON_BIN,
        BRIDGE_FILE,
    ]


    if EARLY_OUTCOME_TRACKER_ENABLED:
        required.append(
            OUTCOME_TRACKER_FILE
        )


    if EARLY_V2_SHADOW_ENABLED:
        required.append(
            V2_SHADOW_FILE
        )


    if EARLY_V2_OUTCOME_ENABLED:
        required.append(
            V2_OUTCOME_TRACKER_FILE
        )


    for path in required:
        if not path.exists():
            raise RuntimeError(
                f"Required file not found: "
                f"{path}"
            )


# ============================================================
# CHILD
# ============================================================

def run_child(
    command,
    label,
):
    global _current_process


    print(
        f"{label} START",
        flush=True,
    )


    env = os.environ.copy()


    try:
        _current_process = subprocess.Popen(
            command,
            cwd=
                str(
                    BASE_DIR
                ),
            env=
                env,
        )


        return_code = (
            _current_process.wait()
        )


    finally:
        _current_process = None


    print(
        f"{label} END | "
        f"return_code={return_code}",
        flush=True,
    )


    return return_code


# ============================================================
# COMPONENTS
# ============================================================

def run_bridge():
    return run_child(
        [
            str(
                PYTHON_BIN
            ),
            str(
                BRIDGE_FILE
            ),
        ],
        "AI BRIDGE",
    )


def run_v2_shadow():
    if not EARLY_V2_SHADOW_ENABLED:
        return 0


    return run_child(
        [
            str(
                PYTHON_BIN
            ),
            str(
                V2_SHADOW_FILE
            ),
        ],
        "EARLY V2 SHADOW",
    )


def run_outcome_tracker():
    if not EARLY_OUTCOME_TRACKER_ENABLED:
        return 0


    return run_child(
        [
            str(
                PYTHON_BIN
            ),
            str(
                OUTCOME_TRACKER_FILE
            ),
        ],
        "EARLY OUTCOME TRACKER",
    )


def run_v2_outcome_tracker():
    if not EARLY_V2_OUTCOME_ENABLED:
        return 0


    return run_child(
        [
            str(
                PYTHON_BIN
            ),
            str(
                V2_OUTCOME_TRACKER_FILE
            ),
        ],
        "EARLY V2 OUTCOME TRACKER",
    )


# ============================================================
# CYCLE
# ============================================================

def run_cycle(
    cycle_number,
):
    started = time.monotonic()


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

    print(
        f"Started UTC : "
        f"{utc_now().isoformat()}"
    )

    print(
        f"Interval    : "
        f"{SCAN_INTERVAL_SECONDS}s"
    )

    print(
        f"AI Gate     : enabled"
    )

    print(
        f"Early track : "
        f"{EARLY_OUTCOME_TRACKER_ENABLED}"
    )

    print(
        f"V2 shadow   : "
        f"{EARLY_V2_SHADOW_ENABLED}"
    )

    print(
        f"V2 outcomes : "
        f"{EARLY_V2_OUTCOME_ENABLED}",
        flush=True,
    )


    bridge_rc = run_bridge()


    if _stop_requested:
        return (
            bridge_rc,
            time.monotonic()
            -
            started,
        )


    v2_shadow_rc = run_v2_shadow()


    if _stop_requested:
        return (
            v2_shadow_rc,
            time.monotonic()
            -
            started,
        )


    outcome_rc = run_outcome_tracker()


    if _stop_requested:
        return (
            outcome_rc,
            time.monotonic()
            -
            started,
        )


    v2_outcome_rc = (
        run_v2_outcome_tracker()
    )


    final_rc = 0


    for rc in (
        bridge_rc,
        v2_shadow_rc,
    ):
        if rc != 0:
            final_rc = rc

            break


    if (
        final_rc == 0
        and
        EARLY_OUTCOME_TRACKER_FAIL_CLOSED
        and
        outcome_rc != 0
    ):
        final_rc = outcome_rc


    elapsed = (
        time.monotonic()
        -
        started
    )


    print()
    print(
        "CYCLE COMPLETE | "
        f"bridge={bridge_rc} | "
        f"v2_shadow={v2_shadow_rc} | "
        f"tracker={outcome_rc} | "
        f"v2_tracker={v2_outcome_rc} | "
        f"return_code={final_rc} | "
        f"elapsed={elapsed:.1f}s",
        flush=True,
    )


    return (
        final_rc,
        elapsed,
    )


# ============================================================
# SLEEP
# ============================================================

def interruptible_sleep(
    seconds,
):
    deadline = (
        time.monotonic()
        +
        seconds
    )


    while (
        not _stop_requested
        and
        time.monotonic()
        <
        deadline
    ):
        remaining = (
            deadline
            -
            time.monotonic()
        )


        time.sleep(
            min(
                1.0,
                max(
                    0.0,
                    remaining,
                ),
            )
        )


# ============================================================
# MAIN
# ============================================================

def main():
    validate_environment()


    signal.signal(
        signal.SIGTERM,
        request_stop,
    )


    signal.signal(
        signal.SIGINT,
        request_stop,
    )


    print(
        "=============================================================="
    )

    print(
        "TRADINGMAX AI AUTONOMOUS SIGNAL RUNNER"
    )

    print(
        "=============================================================="
    )

    print(
        f"Started UTC  : "
        f"{utc_now().isoformat()}"
    )

    print(
        f"Loop seconds : "
        f"{SCAN_INTERVAL_SECONDS}"
    )

    print(
        f"Failure sleep: "
        f"{FAILURE_SLEEP_SECONDS}"
    )

    print(
        f"Run once     : "
        f"{RUN_ONCE}"
    )

    print(
        f"Bridge       : "
        f"{BRIDGE_FILE}"
    )

    print(
        f"Outcome track: "
        f"{EARLY_OUTCOME_TRACKER_ENABLED}"
    )

    print(
        f"V2 shadow    : "
        f"{EARLY_V2_SHADOW_ENABLED}"
    )

    print(
        f"V2 outcomes  : "
        f"{EARLY_V2_OUTCOME_ENABLED}"
    )

    print(
        "==============================================================",
        flush=True,
    )


    cycle_number = 0


    while not _stop_requested:
        cycle_number += 1


        try:
            (
                return_code,
                elapsed,
            ) = run_cycle(
                cycle_number
            )


        except Exception as exc:
            print(
                "CYCLE ERROR | "
                f"{type(exc).__name__}: "
                f"{exc}",
                flush=True,
            )


            if RUN_ONCE:
                raise


            interruptible_sleep(
                FAILURE_SLEEP_SECONDS
            )


            continue


        if RUN_ONCE:
            if return_code != 0:
                raise RuntimeError(
                    "AI cycle failed "
                    f"with return code "
                    f"{return_code}"
                )

            break


        if return_code != 0:
            sleep_seconds = (
                FAILURE_SLEEP_SECONDS
            )

        else:
            sleep_seconds = max(
                0,
                SCAN_INTERVAL_SECONDS
                -
                elapsed,
            )


        print(
            "NEXT CYCLE | "
            f"in {sleep_seconds:.0f}s",
            flush=True,
        )


        interruptible_sleep(
            sleep_seconds
        )


    print(
        "RUNNER STOPPED",
        flush=True,
    )


if __name__ == "__main__":
    try:
        main()

    except KeyboardInterrupt:
        print(
            "RUNNER STOPPED BY USER",
            flush=True,
        )

    except Exception as exc:
        print(
            "AI SIGNAL RUNNER FAILED | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        raise SystemExit(
            1
        )
