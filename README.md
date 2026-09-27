# TradingMax

TradingMax is an automated trading system that receives signals from TradingView, validates them, applies risk and safety controls, queues them in SQLite, and executes approved trades through Interactive Brokers TWS.

The system is designed to fail closed.

If broker data is missing, stale, inconsistent, or ambiguous, TradingMax should block trading rather than guess.

---

# 1. Architecture

```text
TradingView
    |
    v
Cloudflare Tunnel
    |
    v
FastAPI API
signal_server.py
    |
    +--> Secret validation
    +--> Signal validation
    +--> Signal freshness
    +--> Risk sizing
    +--> Live safety checks
    |
    v
SQLite
trading.db
    |
    v
worker.py
    |
    +--> Atomic queue claim
    +--> Freshness validation
    +--> Execution guard
    +--> TWS connection
    +--> Position policy
    +--> Account-wide open orders
    +--> Market session validation
    +--> Account / P&L validation
    +--> PRELIVE_DRY_RUN gate
    |
    v
Interactive Brokers TWS
    |
    v
Bracket Order
ENTRY + TAKE PROFIT + STOP LOSS
    |
    v
trade_monitor.py
    |
    +--> Order identity
    +--> Execution tracking
    +--> Position lifecycle
    +--> Exit detection
    |
    v
SQLite / Dashboard / System Report
