from datetime import datetime, timezone


class SignalContractError(Exception):
    pass


SUPPORTED_ACTIONS = {
    "BUY",
    "SELL",
}


def normalize_action(
    value
):
    action = str(
        value
        or ""
    ).strip().upper()


    if action not in SUPPORTED_ACTIONS:

        raise SignalContractError(
            (
                "Unsupported action: "
                f"{action or '<empty>'}. "
                "Supported actions are BUY and SELL"
            )
        )


    return action


def trade_direction(
    action
):
    normalized = normalize_action(
        action
    )


    if normalized == "BUY":
        return "LONG"


    return "SHORT"


def validate_price_structure(
    *,
    action,
    entry,
    stop,
    target
):
    normalized = normalize_action(
        action
    )


    try:
        entry = float(
            entry
        )

        stop = float(
            stop
        )

        target = float(
            target
        )


    except (
        TypeError,
        ValueError,
    ) as exc:

        raise SignalContractError(
            "Entry, stop and target must be numeric"
        ) from exc


    if entry <= 0:

        raise SignalContractError(
            "Entry must be greater than zero"
        )


    if stop <= 0:

        raise SignalContractError(
            "Stop must be greater than zero"
        )


    if target <= 0:

        raise SignalContractError(
            "Target must be greater than zero"
        )


    if normalized == "BUY":

        if stop >= entry:

            raise SignalContractError(
                (
                    "For a LONG trade, "
                    "stop must be below entry"
                )
            )


        if target <= entry:

            raise SignalContractError(
                (
                    "For a LONG trade, "
                    "target must be above entry"
                )
            )


    else:

        if stop <= entry:

            raise SignalContractError(
                (
                    "For a SHORT trade, "
                    "stop must be above entry"
                )
            )


        if target >= entry:

            raise SignalContractError(
                (
                    "For a SHORT trade, "
                    "target must be below entry"
                )
            )


    return {
        "action":
            normalized,

        "direction":
            (
                "LONG"
                if normalized == "BUY"
                else "SHORT"
            ),

        "entry":
            entry,

        "stop":
            stop,

        "target":
            target,

        "risk_per_share":
            abs(
                entry
                -
                stop
            ),

        "reward_per_share":
            (
                target
                -
                entry
                if normalized == "BUY"
                else
                entry
                -
                target
            ),
    }


def parse_signal_time(
    value
):
    if value is None:

        raise SignalContractError(
            "signal_time is required"
        )


    if isinstance(
        value,
        (
            int,
            float,
        )
    ):

        timestamp = float(
            value
        )


        #
        # TradingView / JavaScript timestamps may arrive
        # in milliseconds.
        #
        if timestamp > 10_000_000_000:

            timestamp /= 1000.0


        try:

            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc,
            )


        except Exception as exc:

            raise SignalContractError(
                "Invalid numeric signal_time"
            ) from exc


    text = str(
        value
    ).strip()


    if not text:

        raise SignalContractError(
            "signal_time is required"
        )


    #
    # Numeric string
    #
    try:

        numeric = float(
            text
        )


        return parse_signal_time(
            numeric
        )


    except ValueError:
        pass


    #
    # ISO-8601
    #
    try:

        if text.endswith(
            "Z"
        ):

            text = (
                text[:-1]
                +
                "+00:00"
            )


        result = datetime.fromisoformat(
            text
        )


        if result.tzinfo is None:

            result = result.replace(
                tzinfo=timezone.utc
            )


        return result.astimezone(
            timezone.utc
        )


    except Exception as exc:

        raise SignalContractError(
            "Invalid signal_time format"
        ) from exc


def validate_signal_metadata(
    *,
    strategy,
    timeframe,
    signal_time,
    max_age_seconds,
    max_future_skew_seconds,
    action=None,
):
    strategy = (
        strategy
        or ""
    ).strip()


    timeframe = (
        timeframe
        or ""
    ).strip()


    if not strategy:

        raise SignalContractError(
            "strategy is required"
        )


    if len(
        strategy
    ) > 100:

        raise SignalContractError(
            "strategy is too long"
        )


    if not timeframe:

        raise SignalContractError(
            "timeframe is required"
        )


    if len(
        timeframe
    ) > 32:

        raise SignalContractError(
            "timeframe is too long"
        )


    parsed_time = parse_signal_time(
        signal_time
    )


    now = datetime.now(
        timezone.utc
    )


    age_seconds = (
        now
        -
        parsed_time
    ).total_seconds()


    if age_seconds > max_age_seconds:

        raise SignalContractError(
            (
                "Signal is stale: "
                f"{age_seconds:.1f}s old, "
                f"maximum is "
                f"{max_age_seconds}s"
            )
        )


    if (
        age_seconds
        <
        -abs(
            max_future_skew_seconds
        )
    ):

        raise SignalContractError(
            (
                "Signal timestamp is too far "
                "in the future: "
                f"{abs(age_seconds):.1f}s"
            )
        )


    result = {
        "strategy":
            strategy,

        "timeframe":
            timeframe,

        "signal_time":
            parsed_time.isoformat(),

        "signal_age_seconds":
            max(
                0.0,
                age_seconds
            ),
    }


    if action is not None:

        normalized_action = (
            normalize_action(
                action
            )
        )


        result[
            "action"
        ] = normalized_action


        result[
            "direction"
        ] = trade_direction(
            normalized_action
        )


    return result
