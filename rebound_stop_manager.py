"""Manage open microcap_rebound_v1 positions: ratchet the stop and force exits.

broker must provide:
  bars_since(symbol, since) -> list of bar dicts with "high"
  modify_stop(signal, trigger) -> None
  close(signal, reason) -> None
  protection_state(signal) -> "ACTIVE" | "EXITED" | "FLAT" | "MISSING"
Not affected by the kill switch: it only raises stops and closes positions.
"""

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import rebound_exits
import rebound_journal as journal
from trade_state import ACTIVE_STATES, ALLOWED_TRANSITIONS, transition_signal

CLOSE_RETRY_SECONDS = 60
ENTRY_TIMEOUT_SECONDS = 180
_EASTERN = ZoneInfo("America/New_York")
CANCELABLE_ENTRY_STATUSES = frozenset(
    status for status in ACTIVE_STATES
    if "CANCEL_REQUESTED" in ALLOWED_TRANSITIONS.get(status, set())
    and status not in {"CANCEL_UNKNOWN", "UNKNOWN"}
)


def parse_ib_time(value):
    """Parse '20261001 07:43:43 US/Eastern' (or ISO) to an aware datetime; None if invalid."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text[:8].isdigit() and len(text) >= 17:
            suffix = text[17:].strip()
            zone = ZoneInfo(suffix) if suffix else _EASTERN
            return datetime.strptime(text[:17], "%Y%m%d %H:%M:%S").replace(tzinfo=zone)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (ValueError, ZoneInfoNotFoundError):
        return None


def _open_signals(conn):
    placeholders = ",".join("?" * len(journal.TERMINAL_STATUSES))
    return [dict(r) for r in conn.execute(
        f"SELECT * FROM signals WHERE strategy=? "
        f"AND COALESCE(status,'') NOT IN ({placeholders}) "
        "AND entry_fill_price IS NOT NULL AND stop IS NOT NULL",
        (journal.STRATEGY, *sorted(journal.TERMINAL_STATUSES)))]


def _signals_columns(conn):
    return {row["name"] for row in conn.execute("PRAGMA table_info(signals)")}


def _parse_signal_timestamp(value):
    parsed = parse_ib_time(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc)


def _entry_expired(signal, now):
    created = _parse_signal_timestamp(signal.get("created_at") or signal.get("signal_time"))
    if created is None:
        return True, None
    return (now.astimezone(timezone.utc) - created) >= timedelta(seconds=ENTRY_TIMEOUT_SECONDS), created


def _entry_expired_recorded(conn, signal_id):
    try:
        row = conn.execute(
            "SELECT 1 FROM rebound_journal WHERE event='ENTRY_EXPIRED' AND signal_id=? LIMIT 1",
            (signal_id,),
        ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def _request_entry_cancel(db_file, conn, signal, now):
    sid = signal["signal_id"]
    if _entry_expired_recorded(conn, sid):
        return
    created = _parse_signal_timestamp(signal.get("created_at") or signal.get("signal_time"))
    detail = {}
    if created is not None:
        detail["age_seconds"] = int((now.astimezone(timezone.utc) - created).total_seconds())
    journal.record(db_file, "ENTRY_EXPIRED", symbol=signal.get("symbol"), signal_id=sid,
                   reason="ENTRY_TIMEOUT", detail=detail, now=now)
    try:
        transition_signal(
            db_file=db_file,
            signal_id=sid,
            new_status="CANCEL_REQUESTED",
            event_type="CANCEL_REQUESTED",
            source="rebound_stop_manager",
            message="Rebound entry expired before fill",
            payload={"parent_order_id": signal.get("parent_order_id")},
            force=False,
        )
    except Exception as exc:
        journal.record(db_file, "ERROR", symbol=signal.get("symbol"), signal_id=sid,
                       reason="ENTRY_CANCEL_REQUEST_FAILED",
                       detail={"error": repr(exc), "status": signal.get("status")}, now=now)


def _cancel_stale_entries(db_file, conn, now):
    columns = _signals_columns(conn)
    required = {"status", "entry_fill_price", "created_at", "signal_time", "parent_order_id"}
    if not required.issubset(columns):
        return
    if not CANCELABLE_ENTRY_STATUSES:
        return
    placeholders = ",".join("?" * len(CANCELABLE_ENTRY_STATUSES))
    rows = conn.execute(
        f"SELECT * FROM signals WHERE strategy=? AND entry_fill_price IS NULL "
        f"AND status IN ({placeholders}) AND parent_order_id IS NOT NULL",
        (journal.STRATEGY, *sorted(CANCELABLE_ENTRY_STATUSES)),
    ).fetchall()
    for row in rows:
        signal = dict(row)
        expired, _ = _entry_expired(signal, now)
        if expired:
            _request_entry_cancel(db_file, conn, signal, now)


def _ensure_position(db_file, conn, signal, now):
    row = conn.execute("SELECT * FROM rebound_positions WHERE signal_id=?",
                       (signal["signal_id"],)).fetchone()
    if row:
        return dict(row)
    entry = float(signal["entry_fill_price"])
    stop = float(signal["stop"])
    opened = parse_ib_time(signal.get("entry_time")) or now
    position = {
        "signal_id": signal["signal_id"], "symbol": signal["symbol"], "entry": entry,
        "initial_stop": stop, "current_stop": stop, "high_since_entry": entry,
        "opened_at": opened.isoformat(), "state": "OPEN", "close_reason": None,
        "updated_at": now.isoformat(),
    }
    conn.execute(
        "INSERT INTO rebound_positions VALUES (:signal_id,:symbol,:entry,:initial_stop,:current_stop,"
        ":high_since_entry,:opened_at,:state,:close_reason,:updated_at)", position)
    conn.commit()
    journal.record(db_file, "ENTRY", symbol=signal["symbol"], signal_id=signal["signal_id"],
                   detail={"entry": entry, "stop": stop, "quantity": signal.get("quantity")}, now=now)
    return position


def _update(conn, signal_id, now, **fields):
    fields["updated_at"] = now.isoformat()
    assignments = ",".join(f"{k}=?" for k in fields)
    conn.execute(f"UPDATE rebound_positions SET {assignments} WHERE signal_id=?",
                 (*fields.values(), signal_id))
    conn.commit()


def _record_exits(db_file, conn, now):
    rows = conn.execute(
        "SELECT p.signal_id, p.symbol, s.status, s.exit_fill_price, s.exit_reason, "
        "COALESCE(s.net_realized_pnl, s.realized_pnl) AS pnl, p.close_reason "
        "FROM rebound_positions p LEFT JOIN signals s ON s.signal_id = p.signal_id "
        "WHERE p.state != 'CLOSED'").fetchall()
    for row in rows:
        if row["status"] in journal.TERMINAL_STATUSES:
            _update(conn, row["signal_id"], now, state="CLOSED")
            journal.record(db_file, "EXIT", symbol=row["symbol"], signal_id=row["signal_id"],
                           reason=row["close_reason"] or row["status"],
                           detail={"status": row["status"], "exit_price": row["exit_fill_price"],
                                   "pnl": row["pnl"], "exit_reason": row["exit_reason"]}, now=now)


def _close_or_record_error(db_file, broker, signal, reason, symbol, sid, now):
    try:
        return broker.close(signal, reason)
    except Exception as exc:
        journal.record(db_file, "ERROR", symbol=symbol, signal_id=sid, reason="CLOSE_FAILED",
                       detail={"error": repr(exc), "close_reason": reason}, now=now)
        return None


def _mark_flat_at_broker(db_file, conn, signal, position, now):
    sid, symbol = position["signal_id"], position["symbol"]
    _update(conn, sid, now, state="CLOSED", close_reason="FLAT_AT_BROKER")
    journal.record(db_file, "EXIT", symbol=symbol, signal_id=sid, reason="FLAT_AT_BROKER",
                   detail={"status": signal.get("status"), "exit_price": signal.get("exit_fill_price"),
                           "pnl": signal.get("net_realized_pnl") or signal.get("realized_pnl"),
                           "exit_reason": "FLAT_AT_BROKER"}, now=now)


def tick(db_file, broker, now=None):
    now = now or datetime.now(timezone.utc)
    conn = journal.connect(db_file)
    try:
        _cancel_stale_entries(db_file, conn, now)
        for signal in _open_signals(conn):
            try:
                _manage(db_file, conn, broker, signal, now)
            except Exception as exc:
                journal.record(db_file, "ERROR", symbol=signal.get("symbol"),
                               signal_id=signal.get("signal_id"), reason="MANAGE_FAILED",
                               detail={"error": repr(exc)}, now=now)
        _record_exits(db_file, conn, now)
    finally:
        conn.close()


def _manage(db_file, conn, broker, signal, now):
    position = _ensure_position(db_file, conn, signal, now)
    sid, symbol = position["signal_id"], position["symbol"]
    if position["state"] == "CLOSED":
        return
    if position["state"] == "CLOSING":
        updated = datetime.fromisoformat(position["updated_at"])
        if now - updated >= timedelta(seconds=CLOSE_RETRY_SECONDS):
            _update(conn, sid, now)
            journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                           reason=position["close_reason"], detail={"retry": True}, now=now)
            result = _close_or_record_error(
                db_file, broker, signal, position["close_reason"], symbol, sid, now)
            if result == "FLAT":
                _mark_flat_at_broker(db_file, conn, signal, position, now)
        return
    protection = broker.protection_state(signal)
    if protection == "EXITED":
        return
    if protection == "FLAT":
        _mark_flat_at_broker(db_file, conn, signal, position, now)
        return
    if protection == "MISSING":
        reason = "UNPROTECTED"
        _update(conn, sid, now, state="CLOSING", close_reason=reason)
        journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                       reason=reason, detail={"protection_state": "MISSING"}, now=now)
        result = _close_or_record_error(db_file, broker, signal, reason, symbol, sid, now)
        if result == "FLAT":
            _mark_flat_at_broker(db_file, conn, signal, position, now)
        return
    opened = datetime.fromisoformat(position["opened_at"])
    high = position["high_since_entry"]
    last_price = None
    try:
        bars = broker.bars_since(symbol, opened)
        high = max([high] + [float(b["high"]) for b in bars if b.get("high") is not None])
        if bars:
            close = bars[-1].get("close")
            if not isinstance(close, bool) and isinstance(close, (int, float)):
                close = float(close)
                if math.isfinite(close):
                    last_price = close
    except Exception as exc:
        journal.record(db_file, "ERROR", symbol=symbol, signal_id=sid, reason="BARS_FAILED",
                       detail={"error": repr(exc)}, now=now)
    if high > position["high_since_entry"]:
        _update(conn, sid, now, high_since_entry=high)
    decision = rebound_exits.next_action(
        entry=position["entry"], initial_stop=position["initial_stop"],
        current_stop=position["current_stop"], high_since_entry=high,
        opened_at=opened, now=now, last_price=last_price)
    if decision["action"] == "MOVE_STOP":
        try:
            broker.modify_stop(signal, decision["stop"])
        except Exception as exc:
            journal.record(db_file, "ERROR", symbol=symbol, signal_id=sid,
                           reason="STOP_MODIFY_FAILED", detail={"error": repr(exc)}, now=now)
            reason = "STOP_MODIFY_FAILED"
            _update(conn, sid, now, state="CLOSING", close_reason=reason)
            journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                           reason=reason, detail={"high": high}, now=now)
            result = _close_or_record_error(db_file, broker, signal, reason, symbol, sid, now)
            if result == "FLAT":
                _mark_flat_at_broker(db_file, conn, signal, position, now)
            return
        _update(conn, sid, now, current_stop=decision["stop"])
        journal.record(db_file, "STOP_MOVE", symbol=symbol, signal_id=sid,
                       detail={"from": position["current_stop"], "to": decision["stop"], "high": high},
                       now=now)
    elif decision["action"] == "CLOSE":
        reason = decision["reason"]
        _update(conn, sid, now, state="CLOSING", close_reason=reason)
        journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                       reason=reason, detail={"high": high}, now=now)
        result = _close_or_record_error(db_file, broker, signal, reason, symbol, sid, now)
        if result == "FLAT":
            _mark_flat_at_broker(db_file, conn, signal, position, now)


def has_work(db_file, now=None):
    """Cheap read-only check so the worker only connects to IBKR when needed."""
    import sqlite3
    now = now or datetime.now(timezone.utc)
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
        conn.row_factory = sqlite3.Row
    except sqlite3.Error:
        return False
    try:
        placeholders = ",".join("?" * len(journal.TERMINAL_STATUSES))
        has_positions_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rebound_positions'"
        ).fetchone() is not None
        closed_position_filter = (
            "AND NOT EXISTS ("
            "SELECT 1 FROM rebound_positions p "
            "WHERE p.signal_id = s.signal_id AND p.state = 'CLOSED')"
            if has_positions_table else ""
        )
        open_signals = conn.execute(
            f"SELECT COUNT(*) FROM signals s WHERE strategy=? "
            f"AND COALESCE(status,'') NOT IN ({placeholders}) "
            f"AND entry_fill_price IS NOT NULL {closed_position_filter}",
            (journal.STRATEGY, *sorted(journal.TERMINAL_STATUSES))).fetchone()[0]
        stale_entries = 0
        columns = _signals_columns(conn)
        required = {"status", "entry_fill_price", "created_at", "signal_time", "parent_order_id"}
        if required.issubset(columns) and CANCELABLE_ENTRY_STATUSES:
            cancel_placeholders = ",".join("?" * len(CANCELABLE_ENTRY_STATUSES))
            rows = conn.execute(
                f"SELECT * FROM signals WHERE strategy=? AND entry_fill_price IS NULL "
                f"AND status IN ({cancel_placeholders}) AND parent_order_id IS NOT NULL",
                (journal.STRATEGY, *sorted(CANCELABLE_ENTRY_STATUSES)),
            ).fetchall()
            stale_entries = sum(
                1 for row in rows
                if _entry_expired(dict(row), now)[0]
                and not _entry_expired_recorded(conn, row["signal_id"])
            )
        try:
            open_positions = conn.execute(
                "SELECT COUNT(*) FROM rebound_positions WHERE state != 'CLOSED'").fetchone()[0]
        except sqlite3.OperationalError:
            open_positions = 0
        return bool(open_signals or stale_entries or open_positions)
    except sqlite3.Error:
        return False
    finally:
        conn.close()
