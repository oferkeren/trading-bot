import os
import time

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


        result = (
            evaluate_candidate(
                context
            )
        )


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

