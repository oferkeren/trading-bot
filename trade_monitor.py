import trade_monitor_core as core


# ============================================================
# TRADINGMAX MONITOR ENTRYPOINT
#
# Broker monitor is deliberately LIVE-only.
#
# PRELIVE signals never own broker orders and therefore must
# never be reconciled against:
#
#     open orders
#     completed orders
#     executions
#     broker positions
#
# PRELIVE lifecycle belongs to worker.py and terminates at:
#
#     PRELIVE_DRY_RUN_BLOCK
# ============================================================


def get_active_live_signals():
    conn = core.db_connect()

    try:
        rows = conn.execute(
            """
            SELECT *
            FROM signals

            WHERE
                test_mode = 0

                AND execution_mode = 'LIVE'

                AND status IN (
                    'PROCESSING',
                    'SUBMITTED',
                    'ACCEPTED_WAITING_MARKET',
                    'FILLED',
                    'OPEN_POSITION',

                    'CANCEL_REQUESTED',
                    'CANCELLING',
                    'CANCEL_PENDING',
                    'CANCEL_UNKNOWN',

                    'UNKNOWN',
                    'ERROR'
                )

            ORDER BY created_at ASC
            """
        ).fetchall()

        return [
            dict(row)
            for row in rows
        ]

    finally:
        conn.close()


# ============================================================
# INSTALL ISOLATION
# ============================================================

core.get_active_signals = (
    get_active_live_signals
)


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    print(
        "TradingMax monitor wrapper started | "
        "broker_monitoring=LIVE_ONLY",
        flush=True
    )

    core.main()
