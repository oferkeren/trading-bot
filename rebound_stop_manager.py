"""Manage open microcap_rebound_v1 positions: ratchet the stop and force exits.

broker must provide:
  bars_since(symbol, since) -> list of bar dicts with "high"
  modify_stop(signal, trigger) -> None
  close(signal, reason) -> None
Not affected by the kill switch: it only raises stops and closes positions.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import rebound_exits
import rebound_journal as journal

CLOSE_RETRY_SECONDS = 60
_EASTERN = ZoneInfo("America/New_York")


def parse_ib_time(value):
    """Parse '20261001 07:43:43 US/Eastern' (or ISO) to an aware datetime; None if invalid."""
    if not value:
        return None
    text = str(value).strip()
    try:
        if text[:8].isdigit() and len(text) >= 17:
            return datetime.strptime(text[:17], "%Y%m%d %H:%M:%S").replace(tzinfo=_EASTERN)
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _open_signals(conn):
    return [dict(r) for r in conn.execute(
        "SELECT * FROM signals WHERE strategy=? AND status='OPEN_POSITION' "
        "AND entry_fill_price IS NOT NULL AND stop IS NOT NULL", (journal.STRATEGY,))]


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


def tick(db_file, broker, now=None):
    now = now or datetime.now(timezone.utc)
    conn = journal.connect(db_file)
    try:
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
    if position["state"] == "CLOSING":
        updated = datetime.fromisoformat(position["updated_at"])
        if now - updated >= timedelta(seconds=CLOSE_RETRY_SECONDS):
            _update(conn, sid, now)
            broker.close(signal, position["close_reason"])
            journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                           reason=position["close_reason"], detail={"retry": True}, now=now)
        return
    opened = datetime.fromisoformat(position["opened_at"])
    high = position["high_since_entry"]
    try:
        bars = broker.bars_since(symbol, opened)
        high = max([high] + [float(b["high"]) for b in bars if b.get("high") is not None])
    except Exception as exc:
        journal.record(db_file, "ERROR", symbol=symbol, signal_id=sid, reason="BARS_FAILED",
                       detail={"error": repr(exc)}, now=now)
    if high > position["high_since_entry"]:
        _update(conn, sid, now, high_since_entry=high)
    decision = rebound_exits.next_action(
        entry=position["entry"], initial_stop=position["initial_stop"],
        current_stop=position["current_stop"], high_since_entry=high,
        opened_at=opened, now=now)
    if decision["action"] == "MOVE_STOP":
        broker.modify_stop(signal, decision["stop"])
        _update(conn, sid, now, current_stop=decision["stop"])
        journal.record(db_file, "STOP_MOVE", symbol=symbol, signal_id=sid,
                       detail={"from": position["current_stop"], "to": decision["stop"], "high": high},
                       now=now)
    elif decision["action"] == "CLOSE":
        broker.close(signal, decision["reason"])
        _update(conn, sid, now, state="CLOSING", close_reason=decision["reason"])
        journal.record(db_file, "CLOSE_REQUEST", symbol=symbol, signal_id=sid,
                       reason=decision["reason"], detail={"high": high}, now=now)


def has_work(db_file):
    """Cheap read-only check so the worker only connects to IBKR when needed."""
    import sqlite3
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error:
        return False
    try:
        open_signals = conn.execute(
            "SELECT COUNT(*) FROM signals WHERE strategy=? AND status='OPEN_POSITION'",
            (journal.STRATEGY,)).fetchone()[0]
        try:
            open_positions = conn.execute(
                "SELECT COUNT(*) FROM rebound_positions WHERE state != 'CLOSED'").fetchone()[0]
        except sqlite3.OperationalError:
            open_positions = 0
        return bool(open_signals or open_positions)
    except sqlite3.Error:
        return False
    finally:
        conn.close()
