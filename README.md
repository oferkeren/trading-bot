# TradingMax

מערכת מסחר אוטומטית המבוססת על:

- TradingView
- Cloudflare Tunnel
- FastAPI
- SQLite
- Python worker
- Interactive Brokers TWS
- IBKR API
- Trade lifecycle monitor
- Runtime status collector
- Watchdog
- systemd

המערכת בנויה בגישת fail-closed.

אם אחד מרכיבי הבטיחות אינו תקין, המערכת אמורה לחסום יצירת עסקה ולא לנסות "להסתדר לבד".

---

# 1. Architecture

```text
TradingView
    |
    v
https://trade.tradingmax.bid
    |
    v
Cloudflare Tunnel
    |
    v
FastAPI
signal_server.py
    |
    +--> authentication
    +--> signal validation
    +--> signal freshness
    +--> risk sizing
    +--> execution safety
    |
    v
SQLite
trading.db
    |
    v
worker.py
    |
    +--> atomic queue claim
    +--> execution freshness
    +--> safety guard
    +--> market-session guard
    +--> position policy
    +--> broker-wide open-order check
    +--> account/P&L validation
    +--> PRELIVE_DRY_RUN gate
    |
    v
IBKR TWS
    |
    v
Bracket Order
ENTRY + TP + SL
    |
    v
trade_monitor.py
    |
    +--> orderRef matching
    +--> permId matching
    +--> orderId matching
    +--> broker parentId matching
    +--> executions
    +--> lifecycle
    |
    v
SQLite / Dashboard / system_report.py
