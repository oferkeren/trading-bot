import threading
import time

from datetime import datetime, time as datetime_time
from zoneinfo import ZoneInfo

import strategy_engine


STRATEGY_NAME = "scalp_pingpong_v1"
TIMEFRAME = "1m"

HISTORICAL_DURATION = "1 D"
HISTORICAL_BAR_SIZE = "1 min"
HISTORICAL_TIMEOUT_SECONDS = 15

MIN_PRICE = 0.50
MAX_PRICE = 50.00

MIN_RANGE_5M_PCT = 3.0
MIN_FLIPS = 3
MIN_MAX_1M_MOVE_PCT = 1.0
MAX_SPREAD_PCT = 0.80

MIN_DOLLAR_VOLUME_5M = 100_000.0
MIN_LIVE_REVERSAL_PCT = 0.12

NEW_YORK = ZoneInfo("America/New_York")
SESSION_START = datetime_time(4, 0)
SESSION_END = datetime_time(20, 0)


def clamp(value, low, high):
    return max(
        low,
        min(
            high,
            value,
        ),
    )


def session_allowed():
    now = datetime.now(
        NEW_YORK
    ).time()

    return (
        SESSION_START
        <= now
        < SESSION_END
    )


def request_history(
    app,
    candidate,
    req_id,
):
    event = threading.Event()

    app.historical_events[
        req_id
    ] = event

    app.historical_bars[
        req_id
    ] = []

    app.reqHistoricalData(
        req_id,
        strategy_engine.make_contract(
            candidate
        ),
        "",
        HISTORICAL_DURATION,
        HISTORICAL_BAR_SIZE,
        "TRADES",
        0,          # include extended hours
        2,
        False,
        [],
    )

    completed = event.wait(
        timeout=
            HISTORICAL_TIMEOUT_SECONDS
    )

    if not completed:
        print(
            "SCALP HISTORICAL TIMEOUT | "
            f"{candidate['symbol']}",
            flush=True,
        )

        try:
            app.cancelHistoricalData(
                req_id
            )
        except Exception:
            pass

    return strategy_engine.completed_bars(
        app.historical_bars.get(
            req_id,
            [],
        )
    )


def bar_direction(bar_a, bar_b):
    a = float(
        bar_a.get(
            "close",
            0,
        )
        or 0
    )

    b = float(
        bar_b.get(
            "close",
            0,
        )
        or 0
    )

    if b > a:
        return 1

    if b < a:
        return -1

    return 0


def count_flips(bars):
    directions = []

    for i in range(
        1,
        len(bars),
    ):
        direction = bar_direction(
            bars[i - 1],
            bars[i],
        )

        if direction != 0:
            directions.append(
                direction
            )

    flips = 0

    for i in range(
        1,
        len(directions),
    ):
        if (
            directions[i]
            !=
            directions[i - 1]
        ):
            flips += 1

    return flips


def one_minute_moves(bars):
    moves = []

    for i in range(
        1,
        len(bars),
    ):
        previous = float(
            bars[i - 1].get(
                "close",
                0,
            )
            or 0
        )

        current = float(
            bars[i].get(
                "close",
                0,
            )
            or 0
        )

        if previous <= 0:
            continue

        moves.append(
            abs(
                (
                    current
                    -
                    previous
                )
                /
                previous
                *
                100.0
            )
        )

    return moves


def volume_acceleration(bars):
    if len(bars) < 6:
        return 0.0

    previous = [
        float(
            item.get(
                "volume",
                0,
            )
            or 0
        )
        for item in bars[-6:-1]
    ]

    previous = [
        item
        for item in previous
        if item > 0
    ]

    if not previous:
        return 0.0

    latest = float(
        bars[-1].get(
            "volume",
            0,
        )
        or 0
    )

    average = (
        sum(previous)
        /
        len(previous)
    )

    if average <= 0:
        return 0.0

    return (
        latest
        /
        average
    )


def analyze(
    candidate,
    bars,
    quote,
):
    if len(bars) < 8:
        return None

    price, price_source = (
        strategy_engine.live_price(
            quote
        )
    )

    spread = (
        strategy_engine.spread_percent(
            quote
        )
    )

    try:
        quote_age = (
            strategy_engine
            .quote_age_seconds(
                quote
            )
        )
    except Exception:
        quote_age = None

    recent = bars[-6:]
    last5 = bars[-5:]

    highs = [
        float(
            bar.get(
                "high",
                0,
            )
            or 0
        )
        for bar in last5
    ]

    lows = [
        float(
            bar.get(
                "low",
                0,
            )
            or 0
        )
        for bar in last5
    ]

    highs = [
        value
        for value in highs
        if value > 0
    ]

    lows = [
        value
        for value in lows
        if value > 0
    ]

    range_5m_pct = 0.0

    if highs and lows:
        high = max(highs)
        low = min(lows)

        midpoint = (
            high + low
        ) / 2.0

        if midpoint > 0:
            range_5m_pct = (
                (
                    high
                    -
                    low
                )
                /
                midpoint
                *
                100.0
            )

    flips = count_flips(
        recent
    )

    moves = one_minute_moves(
        recent
    )

    max_1m_move_pct = (
        max(moves)
        if moves
        else 0.0
    )

    vol_accel = (
        volume_acceleration(
            recent
        )
    )

    volume_5m = sum(
        float(
            item.get(
                "volume",
                0,
            )
            or 0
        )
        for item in last5
    )

    dollar_volume_5m = (
        volume_5m
        *
        float(
            price
            or 0
        )
    )

    hard_failures = []

    if not session_allowed():
        hard_failures.append(
            "SESSION"
        )

    if price is None:
        hard_failures.append(
            "LIVE_PRICE"
        )

    elif not (
        MIN_PRICE
        <= price
        <= MAX_PRICE
    ):
        hard_failures.append(
            "PRICE"
        )

    market_data_type = (
        quote.get(
            "market_data_type"
        )
    )

    if (
        market_data_type
        is not None
        and
        market_data_type != 1
    ):
        hard_failures.append(
            "LIVE_DATA"
        )

    if (
        quote_age is None
        or
        quote_age > 15.0
    ):
        hard_failures.append(
            "STALE_QUOTE"
        )

    if spread is None:
        hard_failures.append(
            "NO_SPREAD"
        )

    elif (
        spread
        >
        MAX_SPREAD_PCT
    ):
        hard_failures.append(
            "SPREAD"
        )

    if (
        dollar_volume_5m
        <
        MIN_DOLLAR_VOLUME_5M
    ):
        hard_failures.append(
            "DOLLAR_VOLUME"
        )

    if (
        range_5m_pct
        <
        MIN_RANGE_5M_PCT
    ):
        hard_failures.append(
            "RANGE_5M"
        )

    if flips < MIN_FLIPS:
        hard_failures.append(
            "FLIPS"
        )

    if (
        max_1m_move_pct
        <
        MIN_MAX_1M_MOVE_PCT
    ):
        hard_failures.append(
            "SPEED"
        )

    last_close = float(
        recent[-1].get(
            "close",
            0,
        )
        or 0
    )

    previous_close = float(
        recent[-2].get(
            "close",
            0,
        )
        or 0
    )

    last_direction = 0

    if last_close > previous_close:
        last_direction = 1

    elif last_close < previous_close:
        last_direction = -1

    live_move_pct = 0.0

    if (
        price is not None
        and
        last_close > 0
    ):
        live_move_pct = (
            (
                price
                -
                last_close
            )
            /
            last_close
            *
            100.0
        )

    action = None

    #
    # Ping-pong reversal:
    #
    # Last completed 1m bar DOWN,
    # live price turns back UP => BUY.
    #
    # Last completed 1m bar UP,
    # live price turns back DOWN => SELL.
    #
    if (
        last_direction < 0
        and
        live_move_pct
        >=
        MIN_LIVE_REVERSAL_PCT
    ):
        action = "BUY"

    elif (
        last_direction > 0
        and
        live_move_pct
        <=
        -MIN_LIVE_REVERSAL_PCT
    ):
        action = "SELL"

    if action is None:
        hard_failures.append(
            "REVERSAL_TRIGGER"
        )

    atr_value = None

    try:
        atr_value = (
            strategy_engine.atr(
                bars,
                14,
            )
        )
    except Exception:
        atr_value = None

    atr_pct = 0.0

    if (
        atr_value is not None
        and
        price
        and
        price > 0
    ):
        atr_pct = (
            atr_value
            /
            price
            *
            100.0
        )

    stop_pct = clamp(
        atr_pct * 0.45,
        0.60,
        1.20,
    )

    target_pct = clamp(
        atr_pct * 0.75,
        1.00,
        2.20,
    )

    entry = price
    stop = None
    target = None

    if (
        entry is not None
        and
        action == "BUY"
    ):
        stop = (
            entry
            *
            (
                1.0
                -
                stop_pct
                /
                100.0
            )
        )

        target = (
            entry
            *
            (
                1.0
                +
                target_pct
                /
                100.0
            )
        )

    elif (
        entry is not None
        and
        action == "SELL"
    ):
        stop = (
            entry
            *
            (
                1.0
                +
                stop_pct
                /
                100.0
            )
        )

        target = (
            entry
            *
            (
                1.0
                -
                target_pct
                /
                100.0
            )
        )

    spread_score = 0.0

    if spread is not None:
        spread_score = max(
            0.0,
            10.0
            *
            (
                1.0
                -
                min(
                    spread
                    /
                    MAX_SPREAD_PCT,
                    1.0,
                )
            )
        )

    scalp_score = (
        min(
            range_5m_pct
            * 7.0,
            35.0,
        )
        +
        min(
            flips
            * 10.0,
            30.0,
        )
        +
        min(
            max_1m_move_pct
            * 8.0,
            20.0,
        )
        +
        min(
            vol_accel
            * 5.0,
            10.0,
        )
        +
        spread_score
    )

    qualified = (
        len(
            hard_failures
        )
        == 0
    )

    support_conditions = [
        (
            "RANGE_5M",
            range_5m_pct
            >=
            MIN_RANGE_5M_PCT,
        ),
        (
            "FLIPS",
            flips
            >=
            MIN_FLIPS,
        ),
        (
            "SPEED",
            max_1m_move_pct
            >=
            MIN_MAX_1M_MOVE_PCT,
        ),
        (
            "DOLLAR_VOLUME",
            dollar_volume_5m
            >=
            MIN_DOLLAR_VOLUME_5M,
        ),
        (
            "REVERSAL",
            action is not None,
        ),
    ]

    support = sum(
        1
        for _, passed
        in support_conditions
        if passed
    )

    result = {
        "strategy":
            STRATEGY_NAME,

        "timeframe":
            TIMEFRAME,

        "symbol":
            candidate[
                "symbol"
            ],

        "instrument_type":
            candidate.get(
                "instrument_type"
            ),

        "action":
            action
            or
            (
                "BUY"
                if live_move_pct >= 0
                else "SELL"
            ),

        "qualified":
            qualified,

        "hard_pass":
            qualified,

        "hard_failures":
            hard_failures,

        "support":
            support,

        "support_total":
            len(
                support_conditions
            ),

        "support_conditions":
            support_conditions,

        "entry":
            entry,

        "stop":
            stop,

        "target":
            target,

        "price_source":
            price_source,

        "spread_pct":
            spread,

        "quote_age_seconds":
            quote_age,

        "range_5m_pct":
            range_5m_pct,

        "flips":
            flips,

        "max_1m_move_pct":
            max_1m_move_pct,

        "volume_acceleration":
            vol_accel,

        "dollar_volume_5m":
            dollar_volume_5m,

        "live_reversal_pct":
            live_move_pct,

        "atr":
            atr_value,

        "stop_pct":
            stop_pct,

        "target_pct":
            target_pct,

        "scalp_score":
            scalp_score,

        "scanner_score":
            candidate.get(
                "score"
            ),

        "effective_score":
            candidate.get(
                "effective_score"
            ),

        "selection_bucket":
            candidate.get(
                "selection_bucket"
            ),
    }

    print(
        "SCALP CHECK | "
        f"{candidate['symbol']:<8} | "
        f"action={result['action']:<4} | "
        f"qualified={qualified} | "
        f"score={scalp_score:.1f} | "
        f"range5m={range_5m_pct:.2f}% | "
        f"flips={flips} | "
        f"max1m={max_1m_move_pct:.2f}% | "
        f"volAccel={vol_accel:.2f} | "
        f"$vol5m={dollar_volume_5m:.0f} | "
        f"spread={spread if spread is not None else 'N/A'} | "
        f"liveRev={live_move_pct:.2f}% | "
        f"hard={hard_failures}",
        flush=True,
    )

    return result
