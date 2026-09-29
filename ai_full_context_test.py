import os

from ai_market_intelligence import (
    CandidateContext,
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

TEST_SYMBOL = (
    os.getenv(
        "AI_FULL_TEST_SYMBOL",
        "AAPL",
    )
    .strip()
    .upper()
)


# ============================================================
# TEST CANDIDATE
# ============================================================

def build_test_candidate(
    symbol,
    market_context,
    news,
):
    #
    # This is NOT an executable trading signal.
    #
    # It is only a realistic candidate payload for
    # testing the AI intelligence layer.
    #

    return CandidateContext(
        symbol=
            symbol,

        action=
            "BUY",

        entry=
            100.00,

        stop=
            98.00,

        target=
            104.00,

        scanner_score=
            85.0,

        effective_score=
            88.0,

        selection_bucket=
            "CORE",

        spread_pct=
            0.20,

        rsi=
            61.0,

        atr=
            2.00,

        volr20=
            2.40,

        vwap=
            99.20,

        market=
            market_context,

        news=
            news,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "========================================"
    )

    print(
        "TRADINGMAX AI FULL CONTEXT TEST"
    )

    print(
        "========================================"
    )

    print(
        "SYMBOL=",
        TEST_SYMBOL
    )


    # ========================================================
    # MARKET CONTEXT
    # ========================================================

    print()
    print(
        "===== MARKET CONTEXT ====="
    )


    market_context, market_details = (
        collect_market_context()
    )


    print(
        market_context.model_dump_json(
            indent=2
        )
    )


    # ========================================================
    # NEWS CONTEXT
    # ========================================================

    print()
    print(
        "===== NEWS CONTEXT ====="
    )


    news = (
        collect_news_context(
            TEST_SYMBOL
        )
    )


    if not news:
        print(
            "NO_RELEVANT_NEWS"
        )

    else:
        for index, item in enumerate(
            news,
            start=1,
        ):
            print(
                f"{index:02d} | "
                f"{item.published_at} | "
                f"{item.source} | "
                f"{item.headline}"
            )


    print()
    print(
        "NEWS_COUNT=",
        len(
            news
        )
    )


    # ========================================================
    # BUILD AI CANDIDATE
    # ========================================================

    candidate = (
        build_test_candidate(
            TEST_SYMBOL,
            market_context,
            news,
        )
    )


    print()
    print(
        "===== AI INPUT ====="
    )


    print(
        candidate.model_dump_json(
            indent=2
        )
    )


    # ========================================================
    # AI
    # ========================================================

    print()
    print(
        "===== AI EVALUATION ====="
    )


    result = (
        evaluate_candidate(
            candidate
        )
    )


    print()
    print(
        "===== AI RESULT ====="
    )


    print(
        result.model_dump_json(
            indent=2
        )
    )


    # ========================================================
    # VALIDATION
    # ========================================================

    print()
    print(
        "===== VALIDATION ====="
    )


    if (
        "AI_UNAVAILABLE"
        in
        result.reason_codes
    ):
        print(
            "AI_FULL_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        result.decision.value
        not in {
            "PASS",
            "REDUCE",
            "BLOCK",
        }
    ):
        print(
            "AI_FULL_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        result.position_multiplier
        not in {
            0.0,
            0.25,
            0.50,
            0.75,
            1.0,
        }
    ):
        print(
            "AI_FULL_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        result.decision.value
        ==
        "PASS"
        and
        result.position_multiplier
        !=
        1.0
    ):
        print(
            "AI_FULL_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    if (
        result.decision.value
        ==
        "BLOCK"
        and
        result.position_multiplier
        !=
        0.0
    ):
        print(
            "AI_FULL_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "AI_FULL_CONTEXT_TEST=PASS"
    )


if __name__ == "__main__":
    main()
