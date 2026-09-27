from datetime import datetime, timezone


class SignalContractError(Exception):
    pass


def parse_signal_time(value):
    if value is None:
        raise SignalContractError(
            "signal_time is required"
        )

    if isinstance(
        value,
        (int, float)
    ):
        timestamp = float(value)

        #
        # TradingView / JS timestamps
        # can arrive in milliseconds.
        #
        if timestamp > 10_000_000_000:
            timestamp /= 1000.0

        try:
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            )

        except Exception as exc:
            raise SignalContractError(
                "Invalid numeric signal_time"
            ) from exc


    text = str(value).strip()

    if not text:
        raise SignalContractError(
            "signal_time is required"
        )


    #
    # Numeric string
    #
    try:
        numeric = float(text)

        return parse_signal_time(
            numeric
        )

    except ValueError:
        pass


    #
    # ISO-8601
    #
    try:
        if text.endswith("Z"):
            text = (
                text[:-1]
                + "+00:00"
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
    max_future_skew_seconds
):
    strategy = (
        strategy or ""
    ).strip()

    timeframe = (
        timeframe or ""
    ).strip()


    if not strategy:
        raise SignalContractError(
            "strategy is required"
        )


    if len(strategy) > 100:
        raise SignalContractError(
            "strategy is too long"
        )


    if not timeframe:
        raise SignalContractError(
            "timeframe is required"
        )


    if len(timeframe) > 32:
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
        - parsed_time
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
        < -abs(
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


    return {
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
            )
    }
