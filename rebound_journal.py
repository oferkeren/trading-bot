"""SQLite journal for microcap_rebound_v1 decisions and managed positions."""

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

STRATEGY = "microcap_rebound_v1"
DEFAULT_DB = str(Path(__file__).resolve().with_name("trading.db"))
TERMINAL_STATUSES = frozenset({
    "CLOSED_SL", "CLOSED_TP", "CLOSED", "CANCELLED", "REJECTED",
    "BLOCKED", "TESTED", "ERROR", "EXPIRED",
})
SKIP_DEDUPE_MINUTES = 10


def connect(db_file):
    conn = sqlite3.connect(db_file, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS rebound_journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL, event TEXT NOT NULL, symbol TEXT,
        signal_id TEXT, reason TEXT, detail TEXT)""")
    conn.execute("CREATE INDEX IF NOT EXISTS rebound_journal_ts ON rebound_journal(ts)")
    conn.execute("""CREATE TABLE IF NOT EXISTS rebound_positions (
        signal_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
        entry REAL NOT NULL, initial_stop REAL NOT NULL, current_stop REAL NOT NULL,
        high_since_entry REAL NOT NULL, opened_at TEXT NOT NULL,
        state TEXT NOT NULL, close_reason TEXT, updated_at TEXT NOT NULL)""")
    return conn


def _iso(now):
    return (now or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat(timespec="seconds")


def record(db_file, event, *, symbol=None, signal_id=None, reason=None, detail=None, now=None):
    with connect(db_file) as conn:
        conn.execute(
            "INSERT INTO rebound_journal (ts, event, symbol, signal_id, reason, detail) VALUES (?,?,?,?,?,?)",
            (_iso(now), event, symbol, signal_id, reason,
             json.dumps(detail, sort_keys=True, default=str) if detail is not None else None),
        )


def record_skip(db_file, symbol, reason, detail=None, now=None):
    """Record SKIP unless the same symbol/reason was recorded in the last 10 minutes."""
    now = now or datetime.now(timezone.utc)
    cutoff = _iso(now - timedelta(minutes=SKIP_DEDUPE_MINUTES))
    with connect(db_file) as conn:
        seen = conn.execute(
            "SELECT 1 FROM rebound_journal WHERE event='SKIP' AND symbol=? AND reason=? AND ts>=? LIMIT 1",
            (symbol, reason, cutoff),
        ).fetchone()
    if seen:
        return False
    record(db_file, "SKIP", symbol=symbol, reason=reason, detail=detail, now=now)
    return True


def is_busy(db_file):
    """True when any rebound signal is not terminal. Any read failure counts as busy."""
    try:
        conn = sqlite3.connect(f"file:{db_file}?mode=ro", uri=True, timeout=10)
        try:
            placeholders = ",".join("?" * len(TERMINAL_STATUSES))
            row = conn.execute(
                f"SELECT COUNT(*) FROM signals WHERE strategy=? "
                f"AND COALESCE(status,'') NOT IN ({placeholders})",
                (STRATEGY, *sorted(TERMINAL_STATUSES)),
            ).fetchone()
        finally:
            conn.close()
        return bool(row and row[0])
    except Exception:
        return True


def summary(db_file, limit=50):
    with connect(db_file) as conn:
        events = [dict(r) for r in conn.execute(
            "SELECT ts, event, symbol, signal_id, reason, detail FROM rebound_journal "
            "ORDER BY id DESC LIMIT ?", (int(limit),))]
        positions = [dict(r) for r in conn.execute(
            "SELECT * FROM rebound_positions WHERE state != 'CLOSED' ORDER BY opened_at")]
        try:
            trades = [dict(r) for r in conn.execute(
                "SELECT signal_id, symbol, status, entry_fill_price, exit_fill_price, "
                "COALESCE(net_realized_pnl, realized_pnl) AS pnl, exit_reason, entry_time, exit_time "
                "FROM signals WHERE strategy=? AND status IN ('CLOSED_SL','CLOSED_TP','CLOSED') "
                "ORDER BY rowid DESC LIMIT ?", (STRATEGY, int(limit)))]
        except sqlite3.OperationalError:
            trades = []
    for event in events:
        event["detail"] = json.loads(event["detail"]) if event["detail"] else None
    pnls = [t["pnl"] for t in trades if t["pnl"] is not None]
    stats = {
        "trades": len(trades),
        "wins": sum(1 for p in pnls if p > 0),
        "losses": sum(1 for p in pnls if p <= 0),
        "total_pnl": round(sum(pnls), 2),
    }
    return {"strategy": STRATEGY, "stats": stats, "open_positions": positions,
            "trades": trades, "events": events}
