"""Bounded, read-only IBKR history and news coverage probe."""

from __future__ import annotations

import math
import re
import threading
import time
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from itertools import count

from ibapi.client import EClient
from ibapi.contract import Contract
from ibapi.wrapper import EWrapper

from microcap_history import CoverageError, parse_utc


_MAX_WINDOW_SECONDS = 7 * 24 * 60 * 60
_MAX_TIMEOUT_SECONDS = 120.0
_MAX_HISTORICAL_TICKS = 1000
_MAX_NEWS_RESULTS = 50
_PACING_PAUSE_SECONDS = 1.0
_BENIGN_IBKR_CODES = {2104, 2106, 2107, 2108, 2158}
_PERMISSION_CODES = {354, 10167, 10168, 10189, 10276}
_PACING_CODES = {100, 420}
_CONNECTION_CODES = {502, 504, 1100, 1101, 1102, 1300}
_EPOCH_RE = re.compile(r"^[0-9]{9,10}$")
_EPOCH_MILLIS_RE = re.compile(r"^[0-9]{13}$")
_SESSION_DATE_RE = re.compile(r"^[0-9]{8}$")
_MAX_TIMESTAMP_ERROR_EXAMPLES = 3
# IBKR documents historicalNews time as epoch. Its installed Python decoder exposes a string,
# so only explicit UTC suffixes, zoned ISO-8601, and epoch values establish an instant.
_NEWS_COMPACT_FORMATS = {
    re.compile(r"^[0-9]{8} [0-9]{2}:[0-9]{2}:[0-9]{2}$"): "%Y%m%d %H:%M:%S",
    re.compile(r"^[0-9]{8} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{1,6}$"): "%Y%m%d %H:%M:%S.%f",
    re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}$"): "%Y-%m-%d %H:%M:%S",
    re.compile(
        r"^[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{1,6}$"
    ): "%Y-%m-%d %H:%M:%S.%f",
}
_EXPLICIT_UTC_SUFFIXES = (" UTC", " GMT")
_STOP_REQUEST_REASONS = {
    "REQUEST_TIMEOUT",
    "IBKR_DISCONNECTED",
    "IBKR_PACING_VIOLATION",
    "IBKR_CONNECTION_FAILED",
    "IBKR_NETWORK_LOOP_FAILED",
}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _ibkr_utc_timestamp(value: object) -> str:
    """Accept Unix seconds or an explicitly zoned ISO timestamp, never local time."""
    if isinstance(value, bool):
        raise CoverageError("TIMESTAMP_INVALID: IBKR timestamp is not a valid time")
    if isinstance(value, int):
        if not 9 <= len(str(abs(value))) <= 10:
            raise CoverageError("TIMESTAMP_INVALID: IBKR epoch timestamp is out of range")
        try:
            return _utc_text(datetime.fromtimestamp(value, tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            raise CoverageError("TIMESTAMP_INVALID: IBKR epoch timestamp is out of range") from None
    if isinstance(value, str) and _EPOCH_RE.fullmatch(value):
        try:
            return _utc_text(datetime.fromtimestamp(int(value), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            raise CoverageError("TIMESTAMP_INVALID: IBKR epoch timestamp is out of range") from None
    if not isinstance(value, str):
        raise CoverageError("TIMESTAMP_INVALID: IBKR timestamp has an unsupported format")
    try:
        return _utc_text(parse_utc(value))
    except (CoverageError, OverflowError):
        raise CoverageError(
            "TIMESTAMP_INVALID: IBKR timestamp is ambiguous or lacks an explicit timezone"
        ) from None


def _ibkr_news_timestamp(value: object) -> tuple[str, str]:
    """Return a UTC timestamp and its basis for an IBKR historicalNews time, failing closed."""
    if isinstance(value, bool):
        raise CoverageError("TIMESTAMP_INVALID: IBKR news timestamp is not a valid time")
    text = str(value).strip() if isinstance(value, (int, str)) else None
    if text is None:
        raise CoverageError("TIMESTAMP_INVALID: IBKR news timestamp has an unsupported format")
    if _EPOCH_RE.fullmatch(text):
        return _ibkr_utc_timestamp(text), "EPOCH_SECONDS_UTC"
    if _EPOCH_MILLIS_RE.fullmatch(text):
        try:
            parsed = datetime.fromtimestamp(int(text) / 1000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            raise CoverageError("TIMESTAMP_INVALID: IBKR epoch timestamp is out of range") from None
        return _utc_text(parsed), "EPOCH_MILLIS_UTC"
    compact_text = text
    explicit_utc = False
    for suffix in _EXPLICIT_UTC_SUFFIXES:
        if text.endswith(suffix):
            compact_text = text[: -len(suffix)]
            explicit_utc = True
            break
    for pattern, fmt in _NEWS_COMPACT_FORMATS.items():
        if pattern.fullmatch(compact_text):
            if not explicit_utc:
                raise CoverageError(
                    "NEWS_TIMESTAMP_AMBIGUOUS: IBKR news timestamp has no timezone"
                )
            try:
                parsed = datetime.strptime(compact_text, fmt).replace(tzinfo=timezone.utc)
            except ValueError:
                break
            return _utc_text(parsed), "EXPLICIT_UTC"
    if explicit_utc:
        text = compact_text + "+00:00"
    try:
        return _utc_text(parse_utc(text)), "EXPLICIT_TIMEZONE"
    except CoverageError as exc:
        if "must include a timezone" in str(exc):
            raise CoverageError(
                "NEWS_TIMESTAMP_AMBIGUOUS: IBKR news timestamp has no timezone"
            ) from None
        raise CoverageError(
            "TIMESTAMP_INVALID: IBKR news timestamp is malformed or has an unestablished timezone"
        ) from None


def _record_time(record: Mapping[str, object], label: str) -> str:
    value = record.get("t", record.get("timestamp", record.get("time")))
    if not isinstance(value, str):
        raise CoverageError(f"TIMESTAMP_INVALID: {label} observation needs an ISO-8601 timestamp")
    return _utc_text(parse_utc(value))


def _reason_for_error(error: Mapping[str, object]) -> str | None:
    code = error.get("error_code", error.get("code"))
    try:
        numeric_code = int(code) if code is not None else None
    except (TypeError, ValueError):
        numeric_code = None
    message = str(error.get("error_message", error.get("message", ""))).casefold()
    if numeric_code in _PACING_CODES or "pacing" in message or "throttl" in message:
        return "IBKR_PACING_VIOLATION"
    if numeric_code in _PERMISSION_CODES or "not subscribed" in message \
            or "permission" in message or "no market data" in message:
        return "IBKR_PERMISSION_DENIED"
    if numeric_code in _CONNECTION_CODES or "disconnect" in message:
        return "IBKR_DISCONNECTED"
    if numeric_code is not None:
        return f"IBKR_ERROR_{numeric_code}"
    reason = error.get("reason")
    return str(reason) if isinstance(reason, str) and reason else "IBKR_ERROR"


def _normalise_errors(errors: Sequence[object]) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    for error in errors:
        if isinstance(error, str):
            item: dict[str, object] = {"message": error}
            if error.isupper() and "_" in error:
                item["reason"] = error
        elif isinstance(error, Mapping):
            item = dict(error)
        else:
            raise CoverageError("OBSERVATIONS_INVALID: each error must be a string or object")
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason:
            classified = _reason_for_error(item)
            if classified:
                item["reason"] = classified
        normalized.append(item)
    return normalized


def _normalise_records(
    records: Sequence[object],
    *,
    label: str,
) -> list[dict[str, object]]:
    if isinstance(records, (str, bytes)):
        raise CoverageError(f"OBSERVATIONS_INVALID: {label} must be a sequence of objects")
    result: list[dict[str, object]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            raise CoverageError(f"OBSERVATIONS_INVALID: {label} item {index} must be an object")
        item = dict(record)
        item["t"] = _record_time(item, label)
        result.append(item)
    return result


def _channel_summary(
    records: list[dict[str, object]],
    *,
    channel: str,
    errors: list[dict[str, object]],
    provider_codes: list[str] | None = None,
) -> dict[str, object]:
    channel_errors = [
        error for error in errors
        if error.get("channel") in (channel, "all")
        and error.get("severity") != "notice"
        and error.get("reason")
    ]
    channel_reasons = sorted({str(error["reason"]) for error in channel_errors})
    timestamps = [str(record["t"]) for record in records]

    if channel == "news" and "NEWS_WINDOW_COVERAGE_UNVERIFIED" in channel_reasons:
        status = "unverified"
    elif records and channel_errors:
        status = "partial"
    elif records:
        status = "observed"
    elif channel_errors:
        status = "unavailable"
    elif channel == "news":
        status = "unverified" if provider_codes else "unavailable"
        if not provider_codes:
            channel_reasons.append("NEWS_PROVIDERS_UNAVAILABLE")
    else:
        status = "unavailable"
    if channel == "news" and not records:
        channel_reasons.append("NEWS_COVERAGE_UNVERIFIED")
    elif not records:
        channel_reasons.append(
            "BAR_COVERAGE_UNAVAILABLE" if channel == "bars" else "QUOTE_COVERAGE_UNAVAILABLE"
        )

    result: dict[str, object] = {
        "status": status,
        "count": len(records),
        "first_utc": min(timestamps) if timestamps else None,
        "last_utc": max(timestamps) if timestamps else None,
        "reasons": sorted(set(channel_reasons)),
        "observations": records,
    }
    if channel == "news":
        result["providers"] = list(provider_codes or [])
        result["articles"] = records
    return result


def summarize_observations(
    *,
    bars: Sequence[object],
    quotes: Sequence[object],
    articles: Sequence[object],
    provider_codes: Sequence[str],
    errors: Sequence[object],
) -> dict[str, object]:
    """Summarize observed coverage without treating missing data as negative evidence."""
    if isinstance(provider_codes, (str, bytes)) or any(
        not isinstance(code, str) or not code.strip() for code in provider_codes
    ):
        raise CoverageError("NEWS_PROVIDERS_INVALID: provider codes must be non-empty strings")
    normalized_providers = list(dict.fromkeys(code.strip() for code in provider_codes))
    normalized_errors = _normalise_errors(errors)
    normalized_bars = _normalise_records(bars, label="bar")
    normalized_quotes = _normalise_records(quotes, label="quote")
    normalized_articles = _normalise_records(articles, label="article")

    for article in normalized_articles:
        provider = article.get("provider_code")
        article_id = article.get("article_id")
        if not isinstance(provider, str) or provider not in normalized_providers:
            raise CoverageError("NEWS_PROVENANCE_INVALID: article provider was not returned by IBKR")
        if not isinstance(article_id, str) or not article_id.strip():
            raise CoverageError("NEWS_PROVENANCE_INVALID: article ID is required")

    bars_result = _channel_summary(normalized_bars, channel="bars", errors=normalized_errors)
    quotes_result = _channel_summary(normalized_quotes, channel="quotes", errors=normalized_errors)
    news_result = _channel_summary(
        normalized_articles,
        channel="news",
        errors=normalized_errors,
        provider_codes=normalized_providers,
    )
    reasons = {
        str(reason)
        for channel in (bars_result, quotes_result, news_result)
        for reason in channel["reasons"]
    }
    reasons.update(
        str(error["reason"])
        for error in normalized_errors
        if error.get("severity") != "notice"
        and error.get("reason")
    )
    return {
        "bars": bars_result,
        "quotes": quotes_result,
        "news": news_result,
        "provider_codes": normalized_providers,
        "errors": normalized_errors,
        "reasons": sorted(reasons),
        "timestamp_basis": (
            "UTC-normalized ISO-8601 inputs and IBKR epoch seconds/milliseconds; "
            "IBKR news timestamps require epoch or explicit timezone evidence; "
            "timezone-naive news strings are unavailable"
        ),
    }


def _classify_ibkr_error(code: int, message: str) -> tuple[str | None, str]:
    if code in _BENIGN_IBKR_CODES:
        return None, "notice"
    error = {"error_code": code, "error_message": message}
    return _reason_for_error(error), "error"


class _ProbeApp(EWrapper, EClient):
    def __init__(self) -> None:
        EClient.__init__(self, self)
        self._request_ids = count(1001)
        self._events: dict[int, threading.Event] = {}
        self._request_channels: dict[int, str] = {}
        self._active_channel: str | None = None
        self._contract_details: dict[int, list[object]] = {}
        self._bars: dict[int, list[object]] = {}
        self._ticks: dict[int, list[object]] = {}
        self._articles: dict[int, list[dict[str, object]]] = {}
        self._provider_codes: list[str] = []
        self._provider_event = threading.Event()
        self._news_has_more: dict[int, bool] = {}
        self._errors: list[dict[str, object]] = []
        self._disconnected = False
        self._closing = False

    def next_request(self, channel: str) -> tuple[int, threading.Event]:
        request_id = next(self._request_ids)
        event = threading.Event()
        self._events[request_id] = event
        self._request_channels[request_id] = channel
        return request_id, event

    def _record_error(
        self,
        *,
        request_id: int | None,
        code: int | None,
        message: str,
        reason: str | None,
        severity: str = "error",
        time_raw: object | None = None,
        details: Mapping[str, object] | None = None,
    ) -> None:
        channel = self._request_channels.get(request_id) if request_id is not None else None
        if request_id == -1:
            channel = "global"
        record: dict[str, object] = {
            "request_id": request_id,
            "error_code": code,
            "error_message": message,
            "channel": channel or "global",
            "severity": severity,
        }
        if reason:
            record["reason"] = reason
        if time_raw is not None:
            record["time_raw"] = time_raw
        if details:
            record.update(details)
        self._errors.append(record)
        event = self._events.get(request_id) if request_id is not None else None
        if event:
            event.set()
        if request_id == -1 and (
            code in _CONNECTION_CODES or reason in _STOP_REQUEST_REASONS
        ):
            # Account-wide errors carry no request ID, so wake the active waiter to stop now.
            self._signal_all()

    def _signal_all(self) -> None:
        for event in self._events.values():
            event.set()
        self._provider_event.set()

    def error(
        self,
        reqId: int,
        errorTime: int,
        errorCode: int,
        errorString: str,
        advancedOrderRejectJson: str = "",
    ) -> None:
        reason, severity = _classify_ibkr_error(errorCode, errorString)
        self._record_error(
            request_id=reqId,
            code=errorCode,
            message=errorString,
            reason=reason,
            severity=severity,
        )

    def contractDetails(self, reqId: int, contractDetails: object) -> None:
        self._contract_details.setdefault(reqId, []).append(contractDetails)

    def contractDetailsEnd(self, reqId: int) -> None:
        event = self._events.get(reqId)
        if event:
            event.set()

    def historicalData(self, reqId: int, bar: object) -> None:
        self._bars.setdefault(reqId, []).append(bar)

    def historicalDataEnd(self, reqId: int, start: str, end: str) -> None:
        event = self._events.get(reqId)
        if event:
            event.set()

    def historicalTicksBidAsk(self, reqId: int, ticks: list[object], done: bool) -> None:
        self._ticks.setdefault(reqId, []).extend(ticks)
        if done:
            event = self._events.get(reqId)
            if event:
                event.set()

    def newsProviders(self, newsProviders: list[object]) -> None:
        self._provider_codes = list(dict.fromkeys(
            code for item in newsProviders
            if isinstance((code := getattr(item, "code", None)), str) and code.strip()
        ))
        self._provider_event.set()

    def historicalNews(
        self,
        requestId: int,
        time: str,
        providerCode: str,
        articleId: str,
        headline: str,
    ) -> None:
        self._articles.setdefault(requestId, []).append({
            "time": time,
            "provider_code": providerCode,
            "article_id": articleId,
            "headline": headline,
        })

    def historicalNewsEnd(self, requestId: int, hasMore: bool) -> None:
        self._news_has_more[requestId] = bool(hasMore)
        event = self._events.get(requestId)
        if event:
            event.set()

    def connectionClosed(self) -> None:
        self._disconnected = True
        if not self._closing:
            self._errors.append({
                "request_id": None,
                "error_code": None,
                "error_message": "IBKR connection closed during a request",
                "channel": self._active_channel or "global",
                "reason": "IBKR_DISCONNECTED",
                "severity": "error",
            })
            self._signal_all()

    def run_network_loop(self) -> None:
        try:
            self.run()
        except (OSError, RuntimeError, TypeError, ValueError) as exc:
            self._record_error(
                request_id=None,
                code=None,
                message=f"IBKR network loop failed: {type(exc).__name__}",
                reason="IBKR_NETWORK_LOOP_FAILED",
            )
            self._signal_all()


def _request_wait(
    app: _ProbeApp,
    request_id: int,
    event: threading.Event,
    timeout_seconds: float,
    channel: str,
) -> bool:
    app._active_channel = channel
    if not event.wait(timeout_seconds):
        app._record_error(
            request_id=request_id,
            code=None,
            message=f"{channel} request timed out",
            reason="REQUEST_TIMEOUT",
        )
        return False
    return not app._disconnected


def _must_stop_requests(app: _ProbeApp) -> bool:
    return app._disconnected or any(
        error.get("reason") in _STOP_REQUEST_REASONS for error in app._errors
    )


def _ibkr_request_time(value: datetime) -> str:
    return value.strftime("%Y%m%d %H:%M:%S UTC")


def _ibkr_news_request_time(value: datetime) -> str:
    # reqHistoricalNews documents this date syntax without a timezone field.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _json_number(value: object) -> object:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _bar_prices(bar: object) -> dict[str, object]:
    return {
        "open": _json_number(getattr(bar, "open", None)),
        "high": _json_number(getattr(bar, "high", None)),
        "low": _json_number(getattr(bar, "low", None)),
        "close": _json_number(getattr(bar, "close", None)),
        "volume": _json_number(getattr(bar, "volume", None)),
    }


def _bar_observation(bar: object, interval: str) -> dict[str, object]:
    """Normalize an intraday ibapi BarData; formatDate=2 sends `.date` as epoch seconds."""
    raw = getattr(bar, "date", None)
    if not isinstance(raw, str) or not _EPOCH_RE.fullmatch(raw):
        raise CoverageError(
            "TIMESTAMP_INVALID: IBKR intraday bar date must be epoch seconds (formatDate=2)"
        )
    return {"t": _ibkr_utc_timestamp(raw), "interval": interval, **_bar_prices(bar)}


def _daily_bar_observation(bar: object) -> dict[str, object]:
    """Keep a daily bar's exchange session date; it is not an as-of-available instant."""
    raw = getattr(bar, "date", None)
    session_date = None
    if isinstance(raw, str) and _SESSION_DATE_RE.fullmatch(raw):
        try:
            session_date = datetime.strptime(raw, "%Y%m%d").date()
        except ValueError:
            session_date = None
    if session_date is None:
        raise CoverageError(
            "TIMESTAMP_INVALID: IBKR daily bar date must be a YYYYMMDD session date"
        )
    return {
        "session_date": session_date.isoformat(),
        "date_raw": raw,
        "interval": "1 day",
        "timestamp_basis": "SESSION_DATE_ONLY",
        "decision_time_verified": False,
        **_bar_prices(bar),
    }


def _record_timestamp_failures(
    app: "_ProbeApp",
    request_id: int,
    failures: Sequence[tuple[str, object]],
) -> None:
    """Record one error per distinct message with a count and bounded raw examples."""
    grouped: dict[str, list[object]] = {}
    for message, raw in failures:
        grouped.setdefault(message, []).append(raw)
    for message, raws in grouped.items():
        examples: list[object] = []
        for raw in raws:
            if raw not in examples:
                examples.append(raw)
            if len(examples) >= _MAX_TIMESTAMP_ERROR_EXAMPLES:
                break
        app._record_error(
            request_id=request_id,
            code=None,
            message=message,
            reason="IBKR_TIMESTAMP_AMBIGUOUS",
            details={"count": len(raws), "time_raw_examples": examples},
        )


def _daily_summary(
    records: list[dict[str, object]],
    errors: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    reasons = {"DAILY_BAR_DECISION_TIME_UNVERIFIED"}
    channel_errors = [
        error for error in errors
        if error.get("channel") in ("daily_bars", "all")
        and error.get("severity") != "notice" and error.get("reason")
    ]
    reasons.update(str(error["reason"]) for error in channel_errors)
    if records and channel_errors:
        status = "partial"
    elif records:
        status = "observed"
    else:
        status = "unavailable"
        reasons.add("DAILY_BAR_COVERAGE_UNAVAILABLE")
    dates = [str(record["session_date"]) for record in records]
    return {
        "status": status,
        "count": len(records),
        "first_session_date": min(dates) if dates else None,
        "last_session_date": max(dates) if dates else None,
        "timestamp_basis": "SESSION_DATE_ONLY",
        "decision_time_verified": False,
        "reasons": sorted(reasons),
        "observations": records,
    }


def _tick_observation(tick: object) -> dict[str, object]:
    return {
        "t": _ibkr_utc_timestamp(getattr(tick, "time", None)),
        "bid": _json_number(getattr(tick, "priceBid", None)),
        "ask": _json_number(getattr(tick, "priceAsk", None)),
        "bid_size": _json_number(getattr(tick, "sizeBid", None)),
        "ask_size": _json_number(getattr(tick, "sizeAsk", None)),
    }


def _article_observation(article: Mapping[str, object]) -> dict[str, object]:
    timestamp, basis = _ibkr_news_timestamp(article.get("time"))
    return {
        "t": timestamp,
        "provider_code": article.get("provider_code"),
        "article_id": article.get("article_id"),
        "headline": article.get("headline"),
        "time_raw": article.get("time"),
        "time_basis": basis,
    }


def _within_window(record: Mapping[str, object], start: datetime, end: datetime) -> bool:
    timestamp = record.get("t")
    if not isinstance(timestamp, str):
        raise CoverageError("TIMESTAMP_INVALID: normalized observation timestamp is missing")
    at = parse_utc(timestamp)
    return start <= at < end


def probe_ibkr(
    symbol: str,
    start: str,
    end: str,
    *,
    host: str,
    port: int,
    client_id: int,
    timeout_seconds: float = 10,
) -> dict[str, object]:
    """Probe one stock contract with historical-only IBKR requests."""
    if not isinstance(symbol, str) or not symbol or symbol != symbol.strip():
        raise CoverageError("SYMBOL_INVALID: symbol must be a non-empty trimmed string")
    start_utc = parse_utc(start)
    end_utc = parse_utc(end)
    window_seconds = (end_utc - start_utc).total_seconds()
    if window_seconds <= 0 or window_seconds > _MAX_WINDOW_SECONDS:
        raise CoverageError("RANGE_INVALID: IBKR probe window must be positive and at most 7 days")
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) \
            or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= _MAX_TIMEOUT_SECONDS:
        raise CoverageError("TIMEOUT_INVALID: timeout must be in (0, 120] seconds")
    if not isinstance(host, str) or not host.strip() or not isinstance(port, int) \
            or isinstance(port, bool) or not 1 <= port <= 65535 \
            or not isinstance(client_id, int) or isinstance(client_id, bool) or client_id < 0:
        raise CoverageError("IBKR_CONNECTION_INVALID: host, port, and client ID are invalid")

    app = _ProbeApp()
    network_thread: threading.Thread | None = None
    bars: list[dict[str, object]] = []
    daily_bars: list[dict[str, object]] = []
    quotes: list[dict[str, object]] = []
    articles: list[dict[str, object]] = []
    provider_codes: list[str] = []
    contract_result: dict[str, object] = {"status": "unresolved", "con_id": None}
    quote_truncated = False
    errors = app._errors

    def request_failed(request_id: int, channel: str, exception: Exception) -> None:
        app._record_error(
            request_id=request_id,
            code=None,
            message=f"{channel} request failed: {type(exception).__name__}",
            reason="IBKR_REQUEST_FAILED",
        )

    try:
        app.connect(host, port, client_id)
        if not app.isConnected():
            app._record_error(
                request_id=None,
                code=None,
                message="IBKR connection could not be established",
                reason="IBKR_CONNECTION_FAILED",
            )
        else:
            network_thread = threading.Thread(target=app.run_network_loop, daemon=True)
            network_thread.start()
            contract_request_id, contract_event = app.next_request("contract")
            app._contract_details[contract_request_id] = []
            query = Contract()
            query.symbol = symbol
            query.secType = "STK"
            query.exchange = "SMART"
            query.currency = "USD"
            app._active_channel = "contract"
            try:
                app.reqContractDetails(contract_request_id, query)
            except (OSError, RuntimeError, TypeError, ValueError) as exc:
                request_failed(contract_request_id, "contract", exc)
            resolved = _request_wait(
                app, contract_request_id, contract_event, timeout_seconds, "contract"
            )
            details = app._contract_details.get(contract_request_id, [])
            candidates: dict[int, object] = {}
            for item in details:
                contract = getattr(item, "contract", None)
                con_id = getattr(contract, "conId", 0)
                if getattr(contract, "secType", None) == "STK" \
                        and getattr(contract, "symbol", "").casefold() == symbol.casefold() \
                        and isinstance(con_id, int) and con_id > 0:
                    candidates[con_id] = contract
            if resolved and len(candidates) == 1:
                con_id, resolved_contract = next(iter(candidates.items()))
                contract_result = {
                    "status": "resolved",
                    "con_id": con_id,
                    "symbol": getattr(resolved_contract, "symbol", symbol),
                    "sec_type": "STK",
                    "currency": getattr(resolved_contract, "currency", "USD"),
                    "exchange": getattr(resolved_contract, "exchange", "SMART"),
                    "primary_exchange": getattr(resolved_contract, "primaryExchange", ""),
                }
                duration_days = max(1, math.ceil(window_seconds / 86400))
                duration = f"{duration_days} D"
                end_request_time = _ibkr_request_time(end_utc)
                # Session dates are calendar labels, so match them to the UTC window's dates.
                first_session_date = start_utc.date()
                last_session_date = (end_utc - timedelta(microseconds=1)).date()
                last_request_at = 0.0

                def pace() -> None:
                    nonlocal last_request_at
                    if last_request_at:
                        remaining = _PACING_PAUSE_SECONDS - (time.monotonic() - last_request_at)
                        if remaining > 0:
                            time.sleep(remaining)
                    last_request_at = time.monotonic()

                for interval in ("1 min", "1 hour", "1 day"):
                    if _must_stop_requests(app):
                        break
                    daily = interval == "1 day"
                    request_id, event = app.next_request("daily_bars" if daily else "bars")
                    app._bars[request_id] = []
                    pace()
                    app._active_channel = "daily_bars" if daily else "bars"
                    try:
                        app.reqHistoricalData(
                            request_id,
                            resolved_contract,
                            end_request_time,
                            duration,
                            interval,
                            "TRADES",
                            0,
                            2,
                            False,
                            [],
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        request_failed(request_id, app._active_channel, exc)
                    _request_wait(app, request_id, event, timeout_seconds, app._active_channel)
                    failures: list[tuple[str, object]] = []
                    for bar in app._bars.get(request_id, []):
                        try:
                            if daily:
                                observation = _daily_bar_observation(bar)
                                if first_session_date <= date.fromisoformat(
                                    str(observation["session_date"])
                                ) <= last_session_date:
                                    daily_bars.append(observation)
                            else:
                                observation = _bar_observation(bar, interval)
                                if _within_window(observation, start_utc, end_utc):
                                    bars.append(observation)
                        except CoverageError as exc:
                            failures.append((str(exc), getattr(bar, "date", None)))
                    _record_timestamp_failures(app, request_id, failures)

                if not _must_stop_requests(app):
                    request_id, event = app.next_request("quotes")
                    app._ticks[request_id] = []
                    pace()
                    app._active_channel = "quotes"
                    try:
                        # IBKR rejects requests with both start and end; page forward from start.
                        app.reqHistoricalTicks(
                            request_id,
                            resolved_contract,
                            _ibkr_request_time(start_utc),
                            "",
                            _MAX_HISTORICAL_TICKS,
                            "BID_ASK",
                            0,
                            False,
                            [],
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        request_failed(request_id, "quotes", exc)
                    _request_wait(app, request_id, event, timeout_seconds, "quotes")
                    raw_ticks = app._ticks.get(request_id, [])
                    reached_end = False
                    for tick in raw_ticks:
                        try:
                            observation = _tick_observation(tick)
                            if _within_window(observation, start_utc, end_utc):
                                quotes.append(observation)
                            elif parse_utc(str(observation["t"])) >= end_utc:
                                reached_end = True
                        except CoverageError as exc:
                            app._record_error(
                                request_id=request_id,
                                code=None,
                                message=str(exc),
                                reason="IBKR_TIMESTAMP_AMBIGUOUS",
                            )
                    if len(raw_ticks) >= _MAX_HISTORICAL_TICKS and not reached_end:
                        quote_truncated = True
                        app._record_error(
                            request_id=request_id,
                            code=None,
                            message=(
                                "Historical bid/ask response reached the fixed tick limit "
                                "before the window end"
                            ),
                            reason="QUOTE_RESULTS_TRUNCATED",
                        )

                if not _must_stop_requests(app):
                    pace()
                    app._active_channel = "news_providers"
                    try:
                        app.reqNewsProviders()
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        app._record_error(
                            request_id=None,
                            code=None,
                            message=f"news providers request failed: {type(exc).__name__}",
                            reason="IBKR_REQUEST_FAILED",
                        )
                    if not app._provider_event.wait(timeout_seconds):
                        app._record_error(
                            request_id=None,
                            code=None,
                            message="IBKR news provider request timed out",
                            reason="REQUEST_TIMEOUT",
                            )
                    provider_codes = list(app._provider_codes)

                if provider_codes and not _must_stop_requests(app):
                    request_id, event = app.next_request("news")
                    app._articles[request_id] = []
                    pace()
                    app._active_channel = "news"
                    try:
                        app.reqHistoricalNews(
                            request_id,
                            con_id,
                            "+".join(provider_codes),
                            "",
                            _ibkr_news_request_time(end_utc),
                            _MAX_NEWS_RESULTS,
                            [],
                        )
                    except (OSError, RuntimeError, TypeError, ValueError) as exc:
                        request_failed(request_id, "news", exc)
                    _request_wait(app, request_id, event, timeout_seconds, "news")
                    if app._news_has_more.get(request_id, False):
                        app._record_error(
                            request_id=request_id,
                            code=None,
                            message="IBKR historical news reports additional results",
                            reason="NEWS_RESULTS_TRUNCATED",
                        )
                    for article in app._articles.get(request_id, []):
                        try:
                            observation = _article_observation(article)
                            if _within_window(observation, start_utc, end_utc):
                                articles.append(observation)
                        except CoverageError as exc:
                            message = str(exc)
                            app._record_error(
                                request_id=request_id,
                                code=None,
                                message=message,
                                reason=(
                                    "NEWS_TIMESTAMP_AMBIGUOUS"
                                    if "NEWS_TIMESTAMP_AMBIGUOUS" in message
                                    else "IBKR_TIMESTAMP_AMBIGUOUS"
                                ),
                                time_raw=article.get("time"),
                            )
                    app._record_error(
                        request_id=request_id,
                        code=None,
                        message=(
                            "IBKR reqHistoricalNews does not document timezone interpretation "
                            "for its date string; local filtering cannot verify request coverage"
                        ),
                        reason="NEWS_WINDOW_COVERAGE_UNVERIFIED",
                    )
            else:
                app._record_error(
                    request_id=contract_request_id,
                    code=None,
                    message=(
                        "IBKR returned no unique active stock contract"
                        if resolved else "IBKR contract lookup did not complete"
                    ),
                    reason="CONTRACT_UNRESOLVED",
                )
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        app._record_error(
            request_id=None,
            code=None,
            message=f"IBKR connection or client failed: {type(exc).__name__}",
            reason="IBKR_CONNECTION_FAILED",
        )
    finally:
        app._closing = True
        try:
            app.disconnect()
        except (OSError, RuntimeError) as exc:
            app._record_error(
                request_id=None,
                code=None,
                message=f"IBKR client disconnect failed: {type(exc).__name__}",
                reason="IBKR_DISCONNECT_FAILED",
            )
        if network_thread and network_thread.is_alive():
            network_thread.join(timeout=1.0)

    result = summarize_observations(
        bars=bars,
        quotes=quotes,
        articles=articles,
        provider_codes=provider_codes,
        errors=errors,
    )
    result["contract"] = contract_result
    result["quotes"]["truncated"] = quote_truncated
    if contract_result["status"] != "resolved":
        result["reasons"] = sorted(set(result["reasons"]) | {"CONTRACT_UNRESOLVED"})
        for channel in ("bars", "quotes", "news"):
            result[channel]["reasons"] = sorted(
                set(result[channel]["reasons"]) | {"CONTRACT_UNRESOLVED"}
            )
    result["daily_bars"] = _daily_summary(daily_bars, errors)
    if contract_result["status"] != "resolved":
        result["daily_bars"]["reasons"] = sorted(
            set(result["daily_bars"]["reasons"]) | {"CONTRACT_UNRESOLVED"}
        )
    result["bar_samples"] = {
        "1 min": sum(bar.get("interval") == "1 min" for bar in bars),
        "1 hour": sum(bar.get("interval") == "1 hour" for bar in bars),
        "1 day": len(daily_bars),
    }
    result["request_window"] = {"start_utc": _utc_text(start_utc), "end_utc": _utc_text(end_utc)}
    result["limits"] = {
        "max_window_days": 7,
        "max_bid_ask_ticks": _MAX_HISTORICAL_TICKS,
        "max_news_results": _MAX_NEWS_RESULTS,
        "pacing_pause_seconds": _PACING_PAUSE_SECONDS,
    }
    return result
