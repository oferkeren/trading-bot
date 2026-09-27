from datetime import datetime, timezone
from zoneinfo import ZoneInfo


TRADING_TIMEZONE = "America/New_York"


class TradingClockError(Exception):
    pass


def trading_timezone():
    try:
        return ZoneInfo(
            TRADING_TIMEZONE
        )

    except Exception as exc:
        raise TradingClockError(
            f"Unable to load timezone {TRADING_TIMEZONE}"
        ) from exc


def utc_now():
    return datetime.now(
        timezone.utc
    )


def market_now():
    return utc_now().astimezone(
        trading_timezone()
    )


def trading_date(
    value=None
):
    """
    Returns YYYY-MM-DD according to US/Eastern trading date.

    value may be:
      - None
      - aware datetime
      - ISO-8601 string
    """

    if value is None:
        timestamp = utc_now()

    elif isinstance(
        value,
        datetime
    ):
        timestamp = value

        if timestamp.tzinfo is None:
            raise TradingClockError(
                "Datetime must be timezone aware"
            )

    else:
        text = str(
            value
        ).strip()

        if not text:
            raise TradingClockError(
                "Timestamp is empty"
            )

        if text.endswith(
            "Z"
        ):
            text = (
                text[:-1]
                + "+00:00"
            )

        try:
            timestamp = datetime.fromisoformat(
                text
            )

        except Exception as exc:
            raise TradingClockError(
                f"Invalid timestamp: {value}"
            ) from exc

        if timestamp.tzinfo is None:
            raise TradingClockError(
                "Timestamp must include timezone"
            )


    return timestamp.astimezone(
        trading_timezone()
    ).date().isoformat()


def trading_day_bounds_utc(
    value=None
):
    """
    Returns start/end UTC timestamps corresponding to one
    calendar day in America/New_York.

    This correctly handles EST/EDT transitions.
    """

    tz = trading_timezone()


    if value is None:
        local = market_now()

    elif isinstance(
        value,
        datetime
    ):
        if value.tzinfo is None:
            raise TradingClockError(
                "Datetime must be timezone aware"
            )

        local = value.astimezone(
            tz
        )

    else:
        text = str(
            value
        ).strip()

        if text.endswith(
            "Z"
        ):
            text = (
                text[:-1]
                + "+00:00"
            )

        try:
            parsed = datetime.fromisoformat(
                text
            )

        except Exception as exc:
            raise TradingClockError(
                f"Invalid timestamp: {value}"
            ) from exc

        if parsed.tzinfo is None:
            raise TradingClockError(
                "Timestamp must include timezone"
            )

        local = parsed.astimezone(
            tz
        )


    local_start = datetime(
        local.year,
        local.month,
        local.day,
        0,
        0,
        0,
        tzinfo=tz
    )


    next_date = (
        local_start.date()
        .fromordinal(
            local_start.date().toordinal() + 1
        )
    )


    local_end = datetime(
        next_date.year,
        next_date.month,
        next_date.day,
        0,
        0,
        0,
        tzinfo=tz
    )


    return {
        "trading_date":
            local_start.date().isoformat(),

        "timezone":
            TRADING_TIMEZONE,

        "start_utc":
            local_start.astimezone(
                timezone.utc
            ).isoformat(),

        "end_utc":
            local_end.astimezone(
                timezone.utc
            ).isoformat()
    }
