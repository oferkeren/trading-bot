from datetime import datetime, timezone


class SignalFreshnessError(Exception):
    pass


def parse_signal_time(
    value
):
    if value is None:
        raise SignalFreshnessError(
            "signal_time missing"
        )


    # --------------------------------------------------------
    # UNIX timestamp
    # --------------------------------------------------------

    if isinstance(
        value,
        (int, float)
    ):
        timestamp = float(
            value
        )

        #
        # TradingView / JS may give milliseconds.
        #
        if timestamp > 10_000_000_000:
            timestamp = (
                timestamp / 1000.0
            )

        try:
            return datetime.fromtimestamp(
                timestamp,
                tz=timezone.utc
            )

        except Exception as exc:
            raise SignalFreshnessError(
                "Invalid numeric signal_time"
            ) from exc


    # --------------------------------------------------------
    # STRING
    # --------------------------------------------------------

    text = str(
        value
    ).strip()


    if not text:
        raise SignalFreshnessError(
            "signal_time empty"
        )


    # Numeric string
    try:
        numeric = float(
            text
        )

        if numeric > 10_000_000_000:
            numeric = (
                numeric / 1000.0
            )

        return datetime.fromtimestamp(
            numeric,
            tz=timezone.utc
        )

    except ValueError:
        pass

    except Exception as exc:
        raise SignalFreshnessError(
            "Invalid numeric signal_time"
        ) from exc


    # ISO 8601
    try:
        if text.endswith(
            "Z"
        ):
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
        raise SignalFreshnessError(
            "Invalid ISO signal_time"
        ) from exc


def check_execution_freshness(
    *,
    signal_time,
    max_age_seconds,
    max_future_skew_seconds
):
    parsed = parse_signal_time(
        signal_time
    )


    now = datetime.now(
        timezone.utc
    )


    age_seconds = (
        now
        - parsed
    ).total_seconds()


    if (
        age_seconds
        >
        max_age_seconds
    ):
        raise SignalFreshnessError(
            (
                "Signal stale at execution "
                f"({age_seconds:.1f}s > "
                f"{max_age_seconds}s)"
            )
        )


    if (
        age_seconds
        <
        -abs(
            max_future_skew_seconds
        )
    ):
        raise SignalFreshnessError(
            (
                "Signal timestamp is too far "
                f"in the future "
                f"({age_seconds:.1f}s)"
            )
        )


    return {
        "signal_time":
            parsed.isoformat(),

        "checked_at":
            now.isoformat(),

        "age_seconds":
            age_seconds,

        "max_age_seconds":
            max_age_seconds,

        "max_future_skew_seconds":
            max_future_skew_seconds
    }
