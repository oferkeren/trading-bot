from datetime import datetime
from zoneinfo import ZoneInfo


class MarketSessionError(Exception):
    pass


def _get_timezone(
    timezone_name
):
    if not timezone_name:
        raise MarketSessionError(
            "IBKR market timezone unavailable"
        )

    try:
        return ZoneInfo(
            timezone_name
        )

    except Exception as exc:
        raise MarketSessionError(
            f"Unsupported IBKR timezone: {timezone_name}"
        ) from exc


def _parse_session_datetime(
    raw,
    fallback_date,
    timezone_name
):
    """
    IBKR may return session components as either:

        0930
        20260928:0930

    Both forms are accepted.
    """

    tz = _get_timezone(
        timezone_name
    )

    raw = str(
        raw
    ).strip()


    if ":" in raw:
        try:
            return datetime.strptime(
                raw,
                "%Y%m%d:%H%M"
            ).replace(
                tzinfo=tz
            )

        except ValueError as exc:
            raise MarketSessionError(
                f"Invalid IBKR session datetime: {raw}"
            ) from exc


    try:
        return datetime.strptime(
            f"{fallback_date}:{raw}",
            "%Y%m%d:%H%M"
        ).replace(
            tzinfo=tz
        )

    except ValueError as exc:
        raise MarketSessionError(
            f"Invalid IBKR session time: {raw}"
        ) from exc


def evaluate_liquid_hours(
    *,
    liquid_hours,
    timezone_name,
    now=None
):
    if not liquid_hours:
        raise MarketSessionError(
            "IBKR liquidHours unavailable"
        )


    tz = _get_timezone(
        timezone_name
    )


    if now is None:
        now = datetime.now(
            tz
        )

    else:
        if now.tzinfo is None:
            raise MarketSessionError(
                "Market-session reference time must be timezone aware"
            )

        now = now.astimezone(
            tz
        )


    current_date = now.strftime(
        "%Y%m%d"
    )


    matched_date = False

    parsed_sessions = []


    for raw_day in str(
        liquid_hours
    ).split(
        ";"
    ):
        raw_day = raw_day.strip()

        if not raw_day:
            continue


        if ":" not in raw_day:
            continue


        day_date, session_text = (
            raw_day.split(
                ":",
                1
            )
        )


        day_date = day_date.strip()

        session_text = (
            session_text.strip()
        )


        if day_date != current_date:
            continue


        matched_date = True


        if (
            session_text.upper()
            == "CLOSED"
        ):
            return {
                "is_open":
                    False,

                "reason":
                    "IBKR reports market CLOSED",

                "timezone":
                    timezone_name,

                "local_time":
                    now.isoformat(),

                "liquid_hours":
                    liquid_hours,

                "session_start":
                    None,

                "session_end":
                    None
            }


        for raw_interval in (
            session_text.split(
                ","
            )
        ):
            raw_interval = (
                raw_interval.strip()
            )


            if not raw_interval:
                continue


            if "-" not in raw_interval:
                continue


            start_raw, end_raw = (
                raw_interval.split(
                    "-",
                    1
                )
            )


            try:
                start_dt = (
                    _parse_session_datetime(
                        start_raw,
                        day_date,
                        timezone_name
                    )
                )

                end_dt = (
                    _parse_session_datetime(
                        end_raw,
                        day_date,
                        timezone_name
                    )
                )

            except MarketSessionError:
                continue


            parsed_sessions.append({
                "start":
                    start_dt,

                "end":
                    end_dt
            })


            if (
                start_dt
                <= now
                < end_dt
            ):
                return {
                    "is_open":
                        True,

                    "reason":
                        "Inside IBKR liquid session",

                    "timezone":
                        timezone_name,

                    "local_time":
                        now.isoformat(),

                    "liquid_hours":
                        liquid_hours,

                    "session_start":
                        start_dt.isoformat(),

                    "session_end":
                        end_dt.isoformat()
                }


    if not matched_date:
        return {
            "is_open":
                False,

            "reason":
                (
                    "No IBKR liquid-hours "
                    "entry for current date"
                ),

            "timezone":
                timezone_name,

            "local_time":
                now.isoformat(),

            "liquid_hours":
                liquid_hours,

            "session_start":
                None,

            "session_end":
                None
        }


    if parsed_sessions:

        parsed_sessions.sort(
            key=lambda item:
                item[
                    "start"
                ]
        )


        return {
            "is_open":
                False,

            "reason":
                (
                    "Outside IBKR liquid "
                    "trading session"
                ),

            "timezone":
                timezone_name,

            "local_time":
                now.isoformat(),

            "liquid_hours":
                liquid_hours,

            "session_start":
                parsed_sessions[0][
                    "start"
                ].isoformat(),

            "session_end":
                parsed_sessions[-1][
                    "end"
                ].isoformat()
        }


    raise MarketSessionError(
        (
            "IBKR liquidHours entry found "
            "but no valid session could be parsed"
        )
    )
