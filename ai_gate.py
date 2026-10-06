import os
import time
import json
import re
import tempfile
from pathlib import Path

from ai_market_intelligence import (
    CandidateContext,
    Decision,
    evaluate_candidate,
)

from market_context import (
    collect_market_context,
)

from news_context import (
    collect_news_context,
)


# ============================================================
# CONFIG
# ============================================================

AI_GATE_ENABLED = (
    os.getenv(
        "AI_GATE_ENABLED",
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


AI_GATE_FAIL_CLOSED = (
    os.getenv(
        "AI_GATE_FAIL_CLOSED",
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


AI_MARKET_CACHE_SECONDS = int(
    os.getenv(
        "AI_MARKET_CACHE_SECONDS",
        "120",
    )
)


AI_NEWS_CACHE_SECONDS = int(
    os.getenv(
        "AI_NEWS_CACHE_SECONDS",
        "300",
    )
)


# Rate-limit circuit breaker; shared between bridge subprocesses.
# Disabled by default until the operator deliberately enables fallback.
AI_RATE_LIMIT_SKIP_ENABLED = os.getenv(
    "AI_RATE_LIMIT_SKIP_ENABLED", "false"
).strip().lower() in {"true", "1", "yes", "on"}
AI_RATE_LIMIT_STATE_PATH = Path(__file__).resolve().parent / ".ai_rate_limit_state.json"
AI_RATE_LIMIT_DEFAULT_COOLDOWN = 600
AI_RATE_LIMIT_MAX_COOLDOWN = 1800


def _rate_limit_error(exc):
    """Only provider 429 / explicit rate limit errors qualify for fallback."""
    if getattr(exc, "status_code", None) == 429:
        return True
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    name = type(exc).__name__.lower()
    return "ratelimit" in name or "rate_limit" in name


def _retry_seconds(exc):
    # Provider text example: 'Please try again in 7m28.4s'.
    message = str(exc)
    match = re.search(r"try again in\s+(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?", message, re.I)
    seconds = AI_RATE_LIMIT_DEFAULT_COOLDOWN
    if match and (match.group(1) or match.group(2)):
        seconds = int(match.group(1) or 0) * 60 + float(match.group(2) or 0)
    else:
        response = getattr(exc, "response", None)
        headers = getattr(response, "headers", {}) or {}
        retry_after = headers.get("retry-after", headers.get("Retry-After", ""))
        try:
            seconds = float(retry_after)
        except (ValueError, TypeError):
            pass
    return max(30, min(AI_RATE_LIMIT_MAX_COOLDOWN, int(seconds + 1)))


def _cooldown_remaining():
    try:
        until = float(json.loads(AI_RATE_LIMIT_STATE_PATH.read_text()).get("until", 0))
        return max(0, int(until - time.time() + 0.999))
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return 0


def _open_circuit(seconds):
    until = time.time() + seconds
    fd, tmp = tempfile.mkstemp(dir=str(AI_RATE_LIMIT_STATE_PATH.parent), prefix=".ai_rl_")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump({"until": until}, f)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, AI_RATE_LIMIT_STATE_PATH)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _rate_limit_skip(candidate, seconds):
    # A resource error is NOT an AI approval; the bridge revalidates risk.
    print(f"AI RATE LIMIT SKIP | {candidate.get('symbol')} | cooldown={seconds}s", flush=True)
    return {
        "status": "SKIP", "reason": "AI_RATE_LIMIT",
        "position_multiplier": 0.0, "ai": None, "market": None,
        "news_count": 0, "cooldown_seconds": seconds,
    }


# ============================================================
# CACHE
# ============================================================

_market_cache = {
    "timestamp":
        0.0,

    "context":
        None,

    "details":
        None,
}


_news_cache = {}


# ============================================================
# MARKET CACHE
# ============================================================

def get_market_context():
    now = time.monotonic()


    cached = (
        _market_cache[
            "context"
        ]
    )


    age = (
        now
        -
        _market_cache[
            "timestamp"
        ]
    )


    if (
        cached is not None
        and
        age
        <
        AI_MARKET_CACHE_SECONDS
    ):
        print(
            "AI MARKET CACHE | "
            f"age={age:.1f}s",
            flush=True,
        )

        return (
            cached,
            _market_cache[
                "details"
            ],
        )


    print(
        "AI MARKET CACHE | refresh",
        flush=True,
    )


    context, details = (
        collect_market_context()
    )


    _market_cache[
        "timestamp"
    ] = now

    _market_cache[
        "context"
    ] = context

    _market_cache[
        "details"
    ] = details


    return (
        context,
        details,
    )


# ============================================================
# NEWS CACHE
# ============================================================

def get_news_context(
    symbol,
):
    symbol = (
        symbol
        .strip()
        .upper()
    )


    now = time.monotonic()


    cached = (
        _news_cache.get(
            symbol
        )
    )


    if cached is not None:
        age = (
            now
            -
            cached[
                "timestamp"
            ]
        )


        if (
            age
            <
            AI_NEWS_CACHE_SECONDS
        ):
            print(
                "AI NEWS CACHE | "
                f"{symbol} | "
                f"age={age:.1f}s",
                flush=True,
            )

            return (
                cached[
                    "news"
                ]
            )


    print(
        "AI NEWS CACHE | "
        f"{symbol} | refresh",
        flush=True,
    )


    news = (
        collect_news_context(
            symbol
        )
    )


    _news_cache[
        symbol
    ] = {
        "timestamp":
            now,

        "news":
            news,
    }


    return news


# ============================================================
# DETERMINISTIC MARKET CHECK
# ============================================================

def deterministic_market_state(
    market,
):
    values = [
        value

        for value
        in [
            market.spy_change_pct,
            market.qqq_change_pct,
            market.iwm_change_pct,
        ]

        if value is not None
    ]


    if not values:
        return {
            "average_change_pct":
                None,

            "state":
                "UNKNOWN",
        }


    average = (
        sum(
            values
        )
        /
        len(
            values
        )
    )


    if average >= 1.5:
        state = (
            "STRONG_BULL"
        )

    elif average >= 0.5:
        state = (
            "BULL"
        )

    elif average <= -1.5:
        state = (
            "STRONG_BEAR"
        )

    elif average <= -0.5:
        state = (
            "BEAR"
        )

    else:
        state = (
            "NEUTRAL"
        )


    return {
        "average_change_pct":
            average,

        "state":
            state,
    }


# ============================================================
# BUILD AI CONTEXT
# ============================================================

def build_candidate_context(
    candidate,
    market,
    news,
):
    required = [
        "symbol",
        "action",
        "entry",
        "stop",
        "target",
    ]


    missing = [
        field

        for field
        in required

        if candidate.get(
            field
        )
        is None
    ]


    if missing:
        raise ValueError(
            "AI candidate missing fields: "
            +
            ",".join(
                missing
            )
        )


    return CandidateContext(
        symbol=
            str(
                candidate[
                    "symbol"
                ]
            )
            .strip()
            .upper(),

        action=
            str(
                candidate[
                    "action"
                ]
            )
            .strip()
            .upper(),

        entry=
            float(
                candidate[
                    "entry"
                ]
            ),

        stop=
            float(
                candidate[
                    "stop"
                ]
            ),

        target=
            float(
                candidate[
                    "target"
                ]
            ),

        scanner_score=
            candidate.get(
                "scanner_score"
            ),

        effective_score=
            candidate.get(
                "effective_score"
            ),

        selection_bucket=
            candidate.get(
                "selection_bucket"
            ),

        spread_pct=
            candidate.get(
                "spread_pct"
            ),

        rsi=
            candidate.get(
                "rsi"
            ),

        atr=
            candidate.get(
                "atr"
            ),

        volr20=
            (
                candidate.get(
                    "volr20"
                )
                if
                candidate.get(
                    "volr20"
                )
                is not None
                else
                candidate.get(
                    "rvol"
                )
            ),

        vwap=
            candidate.get(
                "vwap"
            ),

        market=
            market,

        news=
            news,
    )


# ============================================================
# DETERMINISTIC OVERRIDE
# ============================================================

def apply_market_sanity(
    *,
    candidate,
    ai_result,
    market_state,
):
    action = (
        str(
            candidate[
                "action"
            ]
        )
        .strip()
        .upper()
    )


    deterministic_state = (
        market_state[
            "state"
        ]
    )


    #
    # The LLM may describe the regime however it likes.
    #
    # It cannot use an exaggerated regime to increase risk.
    #


    if (
        action
        ==
        "BUY"
        and
        deterministic_state
        in {
            "BEAR",
            "STRONG_BEAR",
        }
    ):
        if (
            ai_result.decision
            ==
            Decision.PASS
        ):
            ai_result.decision = (
                Decision.REDUCE
            )

            ai_result.position_multiplier = (
                0.50
            )


    if (
        action
        ==
        "BUY"
        and
        deterministic_state
        ==
        "STRONG_BEAR"
    ):
        if (
            ai_result.position_multiplier
            >
            0.25
        ):
            ai_result.decision = (
                Decision.REDUCE
            )

            ai_result.position_multiplier = (
                0.25
            )


    if (
        action
        ==
        "SELL"
        and
        deterministic_state
        in {
            "BULL",
            "STRONG_BULL",
        }
    ):
        if (
            ai_result.decision
            ==
            Decision.PASS
        ):
            ai_result.decision = (
                Decision.REDUCE
            )

            ai_result.position_multiplier = (
                0.50
            )


    if (
        ai_result.decision
        ==
        Decision.BLOCK
    ):
        ai_result.position_multiplier = (
            0.0
        )


    return ai_result


# ============================================================
# PUBLIC GATE
# ============================================================

def evaluate_trade_candidate(
    candidate,
):
    if not AI_GATE_ENABLED:
        return {
            "status":
                "BLOCK"
                if AI_GATE_FAIL_CLOSED
                else
                "PASS",

            "reason":
                "AI_GATE_DISABLED",

            "position_multiplier":
                0.0
                if AI_GATE_FAIL_CLOSED
                else
                1.0,

            "ai":
                None,

            "market":
                None,

            "news_count":
                0,
        }


    symbol = (
        str(
            candidate.get(
                "symbol",
                "",
            )
        )
        .strip()
        .upper()
    )


    if not symbol:
        raise ValueError(
            "Candidate symbol missing"
        )


    if AI_RATE_LIMIT_SKIP_ENABLED:
        remaining = _cooldown_remaining()
        if remaining:
            return _rate_limit_skip(candidate, remaining)

    try:
        market, market_details = (
            get_market_context()
        )


        market_state = (
            deterministic_market_state(
                market
            )
        )


        news = (
            get_news_context(
                symbol
            )
        )


        context = (
            build_candidate_context(
                candidate,
                market,
                news,
            )
        )


        try:
            result = (
                evaluate_candidate(
                    context
                )
            )
        except Exception as exc:
            if AI_RATE_LIMIT_SKIP_ENABLED and _rate_limit_error(exc):
                seconds = _retry_seconds(exc)
                _open_circuit(seconds)
                return _rate_limit_skip(candidate, seconds)
            raise


        result = (
            apply_market_sanity(
                candidate=
                    candidate,

                ai_result=
                    result,

                market_state=
                    market_state,
            )
        )


        print(
            "AI GATE | "
            f"{symbol} | "
            f"action="
            f"{candidate.get('action')} | "
            f"decision="
            f"{result.decision.value} | "
            f"multiplier="
            f"{result.position_multiplier:.2f} | "
            f"deterministic_market="
            f"{market_state['state']} | "
            f"avg_market="
            f"{market_state['average_change_pct']} | "
            f"news="
            f"{len(news)}",
            flush=True,
        )


        return {
            "status":
                result.decision.value,

            "position_multiplier":
                result.position_multiplier,

            "market_state":
                market_state,

            "market":
                market.model_dump(
                    mode="json"
                ),

            "news_count":
                len(
                    news
                ),

            "ai":
                result.model_dump(
                    mode="json"
                ),
        }


    except Exception as exc:
        print(
            "AI GATE ERROR | "
            f"{symbol} | "
            f"{type(exc).__name__}: "
            f"{exc}",
            flush=True,
        )

        error_text = str(exc).lower()

        if "news request timeout" in error_text:
            print(
                "AI NEWS TIMEOUT FAIL-OPEN | "
                f"{symbol}",
                flush=True,
            )

            return {
                "status":
                    "PASS",

                "reason":
                    "NEWS_TIMEOUT_FAIL_OPEN",

                "error_type":
                    type(
                        exc
                    ).__name__,

                "position_multiplier":
                    1.0,

                "ai":
                    None,

                "market":
                    None,

                "news_count":
                    0,
            }


        if AI_GATE_FAIL_CLOSED:
            return {
                "status":
                    "BLOCK",

                "reason":
                    "AI_GATE_ERROR",

                "error_type":
                    type(
                        exc
                    ).__name__,

                "position_multiplier":
                    0.0,

                "ai":
                    None,

                "market":
                    None,

                "news_count":
                    0,
            }


        return {
            "status":
                "PASS",

            "reason":
                "AI_GATE_ERROR_FAIL_OPEN",

            "error_type":
                type(
                    exc
                ).__name__,

            "position_multiplier":
                1.0,

            "ai":
                None,

            "market":
                None,

            "news_count":
                0,
        }


# ============================================================
# SELF TEST
# ============================================================

def main():
    candidate = {
        "symbol":
            "AAPL",

        "action":
            "BUY",

        "qualified":
            True,

        "entry":
            100.0,

        "stop":
            98.0,

        "target":
            104.0,

        "scanner_score":
            85.0,

        "effective_score":
            88.0,

        "selection_bucket":
            "CORE",

        "spread_pct":
            0.20,

        "rsi":
            61.0,

        "atr":
            2.0,

        "volr20":
            2.4,

        "vwap":
            99.2,
    }


    print(
        "========================================"
    )

    print(
        "TRADINGMAX AI GATE SELF TEST"
    )

    print(
        "========================================"
    )


    result = (
        evaluate_trade_candidate(
            candidate
        )
    )


    import json


    print()
    print(
        json.dumps(
            result,
            indent=2,
            sort_keys=True,
        )
    )


    print()


    if (
        result[
            "status"
        ]
        not in {
            "PASS",
            "REDUCE",
            "BLOCK",
        }
    ):
        print(
            "AI_GATE_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "AI_GATE_SELF_TEST=PASS"
    )


if __name__ == "__main__":
    main()

