from ai_market_intelligence import (
    CandidateContext,
    evaluate_candidate,
)

from market_context import (
    collect_market_context,
)


def main():
    print(
        "========================================"
    )

    print(
        "TRADINGMAX AI + REAL MARKET CONTEXT"
    )

    print(
        "========================================"
    )


    # ========================================================
    # GET REAL MARKET CONTEXT FROM IBKR
    # ========================================================

    print()
    print(
        "===== COLLECTING MARKET CONTEXT ====="
    )


    market_context, market_details = (
        collect_market_context()
    )


    print()
    print(
        "===== REAL MARKET CONTEXT ====="
    )


    print(
        market_context.model_dump_json(
            indent=2
        )
    )


    print()
    print(
        "===== MARKET DETAILS ====="
    )


    for symbol in [
        "SPY",
        "QQQ",
        "IWM",
        "VIX",
    ]:
        item = (
            market_details[
                symbol
            ]
        )


        print(
            f"{symbol:4} | "
            f"price={item['price']} | "
            f"close={item['close']} | "
            f"change={item['change_pct']}% | "
            f"source={item['price_source']} | "
            f"dataType={item['market_data_type']}"
        )


    # ========================================================
    # BUILD TEST CANDIDATE
    # ========================================================

    #
    # This is NOT a real trading signal.
    #
    # We only want to see how the LLM evaluates
    # a hypothetical LONG candidate under today's
    # actual market conditions.
    #

    candidate = CandidateContext(
        symbol=
            "TMXTEST",

        action=
            "BUY",

        entry=
            10.00,

        stop=
            9.80,

        target=
            10.40,

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
            0.20,

        volr20=
            2.40,

        vwap=
            9.92,

        market=
            market_context,

        #
        # News integration comes next.
        #
        news=[],
    )


    # ========================================================
    # AI EVALUATION
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
            "AI_MARKET_CONTEXT_TEST=FAIL"
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
            "AI_MARKET_CONTEXT_TEST=FAIL"
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
            "AI_MARKET_CONTEXT_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "AI_MARKET_CONTEXT_TEST=PASS"
    )


if __name__ == "__main__":
    main()
