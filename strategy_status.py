import json
import os
import tempfile
import time

from datetime import datetime, timezone
from pathlib import Path


BASE_DIR = Path(
    __file__
).resolve().parent


STATUS_FILE = Path(
    os.getenv(
        "TRADINGMAX_STRATEGY_STATUS_FILE",
        str(
            BASE_DIR
            /
            "strategy_status.json"
        ),
    )
)


STATUS_VERSION = 2


def now_iso():

    return (
        datetime.now(
            timezone.utc
        )
        .isoformat()
    )


def empty_snapshot():

    return {
        "version":
            STATUS_VERSION,

        "updated_at":
            None,

        "heartbeat_epoch":
            None,

        "cycle":
            None,

        "phase":
            "IDLE",

        "session":
            None,

        "directional":
            0,

        "universe":
            0,

        "core":
            0,

        "rotate":
            0,

        "eligible":
            0,

        "hot_pool":
            0,

        "hot_core":
            0,

        "hot_rotate":
            0,

        "analyzed":
            0,

        "qualified":
            0,

        "bridge_dry_run":
            None,

        "bridge_allow_post":
            None,

        "candidates":
            [],

        "qualified_candidates":
            [],

        "last_error":
            None,
    }


def normalize_number(
    value,
):

    if value is None:

        return None


    try:

        return float(
            value
        )


    except Exception:

        return None


def normalize_candidate(
    candidate,
):

    return {
        "symbol":
            candidate.get(
                "symbol"
            ),

        "instrument_type":
            candidate.get(
                "instrument_type"
            ),

        "bias":
            candidate.get(
                "bias"
            ),

        "action":
            candidate.get(
                "action"
            ),

        "bucket":
            candidate.get(
                "selection_bucket"
            ),

        "scanner_score":
            normalize_number(
                candidate.get(
                    "score",
                    candidate.get(
                        "scanner_score"
                    ),
                )
            ),

        "effective_score":
            normalize_number(
                candidate.get(
                    "effective_score"
                )
            ),

        "hot_score":
            normalize_number(
                candidate.get(
                    "hot_score"
                )
            ),

        "distance_pct":
            normalize_number(
                candidate.get(
                    "hot_proxy_distance_pct",
                    candidate.get(
                        "trigger_distance_pct"
                    ),
                )
            ),

        "distance_source":
            candidate.get(
                "hot_distance_source"
            ),

        "spread_pct":
            normalize_number(
                candidate.get(
                    "hot_spread_pct",
                    candidate.get(
                        "spread_pct"
                    ),
                )
            ),

        "max_spread_pct":
            normalize_number(
                candidate.get(
                    "hot_max_spread_pct"
                )
            ),

        "quote_age_seconds":
            normalize_number(
                candidate.get(
                    "hot_quote_age_seconds",
                    candidate.get(
                        "quote_age_seconds"
                    ),
                )
            ),

        "eligible":
            candidate.get(
                "hot_eligible"
            ),

        "qualified":
            candidate.get(
                "qualified"
            ),

        "support":
            candidate.get(
                "support"
            ),

        "support_total":
            candidate.get(
                "support_total"
            ),

        "entry":
            normalize_number(
                candidate.get(
                    "entry"
                )
            ),

        "stop":
            normalize_number(
                candidate.get(
                    "stop"
                )
            ),

        "target":
            normalize_number(
                candidate.get(
                    "target"
                )
            ),

        "hard_failures":
            list(
                candidate.get(
                    "hard_failures"
                )
                or
                []
            ),
    }


def atomic_write(
    payload,
):

    STATUS_FILE.parent.mkdir(
        parents=True,
        exist_ok=True,
    )


    serialized = json.dumps(
        payload,
        indent=2,
        sort_keys=True,
    )


    fd, temporary_path = (
        tempfile.mkstemp(
            prefix=
                ".strategy_status.",

            suffix=
                ".tmp",

            dir=
                str(
                    STATUS_FILE.parent
                ),
        )
    )


    try:

        with os.fdopen(
            fd,
            "w",
            encoding="utf-8",
        ) as handle:

            handle.write(
                serialized
            )

            handle.flush()

            os.fsync(
                handle.fileno()
            )


        os.replace(
            temporary_path,
            STATUS_FILE,
        )


    except Exception:

        try:

            os.unlink(
                temporary_path
            )

        except Exception:

            pass


        raise


def read_snapshot():

    if not STATUS_FILE.exists():

        return (
            empty_snapshot()
        )


    try:

        data = json.loads(
            STATUS_FILE.read_text(
                encoding="utf-8"
            )
        )


        if not isinstance(
            data,
            dict,
        ):

            return (
                empty_snapshot()
            )


        result = (
            empty_snapshot()
        )


        result.update(
            data
        )


        return result


    except Exception as exc:

        result = (
            empty_snapshot()
        )


        result[
            "last_error"
        ] = (
            f"{type(exc).__name__}: "
            f"{exc}"
        )


        return result


def write_snapshot(
    **updates,
):

    snapshot = (
        read_snapshot()
    )


    snapshot.update(
        updates
    )


    snapshot[
        "updated_at"
    ] = now_iso()


    snapshot[
        "heartbeat_epoch"
    ] = time.time()


    snapshot[
        "version"
    ] = STATUS_VERSION


    atomic_write(
        snapshot
    )


    return snapshot


def mark_phase(
    phase,
    **updates,
):

    return write_snapshot(
        phase=
            phase,

        **updates,
    )


def record_bridge_mode(
    dry_run,
    allow_post,
):

    return write_snapshot(
        bridge_dry_run=
            bool(
                dry_run
            ),

        bridge_allow_post=
            bool(
                allow_post
            ),
    )


def record_session(
    session,
):

    return write_snapshot(
        session=
            session,
    )


def record_universe(
    cycle,
    directional,
    selected,
):

    core = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "CORE"
    )


    rotate = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "ROTATE"
    )


    return write_snapshot(
        cycle=
            cycle,

        phase=
            "UNIVERSE",

        directional=
            int(
                directional
            ),

        universe=
            len(
                selected
            ),

        core=
            core,

        rotate=
            rotate,

        eligible=
            0,

        hot_pool=
            0,

        hot_core=
            0,

        hot_rotate=
            0,

        analyzed=
            0,

        qualified=
            0,

        candidates=
            [],

        qualified_candidates=
            [],

        last_error=
            None,
    )


def record_hot_pool(
    ranked_candidates,
    selected,
):

    eligible = sum(
        1

        for item
        in ranked_candidates

        if item.get(
            "hot_eligible"
        )
    )


    hot_core = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "CORE"
    )


    hot_rotate = sum(
        1

        for item
        in selected

        if item.get(
            "selection_bucket"
        )
        ==
        "ROTATE"
    )


    normalized_selected = [
        normalize_candidate(
            item
        )

        for item
        in selected
    ]


    normalized_selected.sort(
        key=lambda item: (
            item.get(
                "hot_score"
            )
            if item.get(
                "hot_score"
            )
            is not None
            else
            -999.0
        ),
        reverse=True,
    )


    return write_snapshot(
        phase=
            "HOT_POOL",

        eligible=
            eligible,

        hot_pool=
            len(
                selected
            ),

        hot_core=
            hot_core,

        hot_rotate=
            hot_rotate,

        candidates=
            normalized_selected,

        analyzed=
            0,

        qualified=
            0,

        qualified_candidates=
            [],
    )


def record_analysis(
    results,
):

    normalized = [
        normalize_candidate(
            item
        )

        for item
        in results
    ]


    qualified = [
        item

        for item
        in normalized

        if item.get(
            "qualified"
        )
    ]


    return write_snapshot(
        phase=
            "ANALYZED",

        analyzed=
            len(
                normalized
            ),

        qualified=
            len(
                qualified
            ),

        qualified_candidates=
            qualified,
    )


def record_error(
    exc,
):

    return write_snapshot(
        phase=
            "ERROR",

        last_error=(
            f"{type(exc).__name__}: "
            f"{exc}"
        ),
    )


def heartbeat():

    return write_snapshot()


if __name__ == "__main__":

    print(
        json.dumps(
            read_snapshot(),
            indent=2,
            sort_keys=True,
        )
    )
