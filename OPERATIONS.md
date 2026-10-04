# TradingMax Operations Runbook

This document describes how to operate, validate, troubleshoot, and safely move TradingMax between TEST, PRELIVE, and LIVE modes.

---

# 1. Daily Startup

Enter the project:

```bash
cd ~/trading-bot
source venv/bin/activate

Run the system report:
python system_report.py

Before any LIVE trading, verify:
All critical services active
All critical watchdog components healthy
TWS connected
Correct IBKR account
Broker snapshot fresh
Daily P/L available
Net liquidation available
Available funds valid
Managed positions below limit
Total broker positions below emergency limit
No unexpected open orders
No unexpected active signals
Kill switch OFF

Do not continue if:
TWS connected : False

Do not continue if any of these are unavailable:
Daily P/L
Net liquidation
Available funds

2. Service Status
Check all TradingMax services:
for service in \
  trading-bot \
  trading-worker \
  trading-monitor \
  trading-status \
  trading-watchdog \
  cloudflared
do
    printf "%-25s " "$service"
    systemctl is-active "$service"
done

Expected:
active
active
active
active
active
active

3. Restarting Services
API:
sudo systemctl restart trading-bot

Worker:
sudo systemctl restart trading-worker

Monitor:
sudo systemctl restart trading-monitor

Status collector:
sudo systemctl restart trading-status

Watchdog:
sudo systemctl restart trading-watchdog

Cloudflare:
sudo systemctl restart cloudflared

After changing a systemd unit or override:
sudo systemctl daemon-reload

4. Logs
API:
sudo journalctl \
  -u trading-bot \
  --since "10 minutes ago" \
  --no-pager

Worker:
sudo journalctl \
  -u trading-worker \
  --since "10 minutes ago" \
  --no-pager

Monitor:
sudo journalctl \
  -u trading-monitor \
  --since "10 minutes ago" \
  --no-pager

Status collector:
sudo journalctl \
  -u trading-status \
  --since "10 minutes ago" \
  --no-pager

Watchdog:
sudo journalctl \
  -u trading-watchdog \
  --since "10 minutes ago" \
  --no-pager

Follow worker live:
sudo journalctl -fu trading-worker

Follow monitor live:
sudo journalctl -fu trading-monitor

5. Trading Modes
TEST
LIVE_TRADING=true
WEBHOOK_TEST_MODE=true
PRELIVE_DRY_RUN=true

Signals should end as:
TESTED

No broker orders should be created.
PRELIVE
LIVE_TRADING=true
WEBHOOK_TEST_MODE=false
PRELIVE_DRY_RUN=true

This uses the real execution validation path.
The worker must stop at:
PRELIVE_DRY_RUN_BLOCK

before order IDs are allocated or orders are submitted.
LIVE
LIVE_TRADING=true
WEBHOOK_TEST_MODE=false
PRELIVE_DRY_RUN=false

This mode can place real orders.
Do not enable LIVE until PRELIVE has passed completely.
6. Check Current Mode
API mode:
curl -s \
  http://127.0.0.1:8000/health \
  | python -m json.tool

Worker mode:
systemctl show trading-worker \
  -p Environment \
  --no-pager

Expected safe development state:
LIVE_TRADING=true
WEBHOOK_TEST_MODE=true
PRELIVE_DRY_RUN=true

7. Worker Environment
Check:
systemctl show trading-worker \
  -p Environment \
  --no-pager

PRELIVE configuration should include:
MAX_MANAGED_POSITIONS=3
MAX_TOTAL_BROKER_POSITIONS=10
PRELIVE_DRY_RUN=true

Worker override:
/etc/systemd/system/trading-worker.service.d/override.conf

Expected:
[Service]
Environment=MAX_MANAGED_POSITIONS=3
Environment=MAX_TOTAL_BROKER_POSITIONS=10
Environment=PRELIVE_DRY_RUN=true

After modification:
sudo systemctl daemon-reload
sudo systemctl restart trading-worker

8. TWS Check
TradingMax LIVE TWS port:
7496

Check:
ss -ltnp | grep ':7496'

If no listener exists, do not trade.
Worker client ID:
10

Monitor client ID:
40

TWS API access must be enabled.
For LIVE order submission, TWS Read-Only API must be disabled.
9. Broker Snapshot Failure
Symptoms:
TWS connected    : False
Broker positions : 0
Daily P/L        : None
Net liquidation  : None
Available funds  : None

This does not mean the account is empty.
It means broker state is unavailable.
Check:
systemctl is-active trading-status

Then:
sudo journalctl \
  -u trading-status \
  --since "10 minutes ago" \
  --no-pager

Check TWS port:
ss -ltnp | grep ':7496'

After recovery:
sleep 10
python ~/trading-bot/system_report.py

Do not resume trading until the snapshot is valid.
10. Position Policy
Current limits:
MAX_MANAGED_POSITIONS=3
MAX_TOTAL_BROKER_POSITIONS=10

Example:
Broker positions  : 6
Managed positions : 0/3
Legacy positions  : 6
Emergency total   : 6/10

MANAGED positions belong to TradingMax.
LEGACY positions existed independently of TradingMax.
Legacy positions do not consume the managed-position quota.
All positions count toward the emergency total-position limit.
11. Market Session
Current policy:
REQUIRE_LIQUID_SESSION=true

When the market is closed, expected events include:
MARKET_SESSION_CLOSED
MARKET_CLOSED_BLOCK

No order should be submitted.
12. Signal Freshness
Current settings:
MAX_SIGNAL_AGE_SECONDS=60
MAX_SIGNAL_FUTURE_SKEW_SECONDS=10

Signal freshness is checked at API ingestion and again immediately before execution.
A stale signal must not execute.
13. Daily Trade Limit
Current limit:
MAX_TRADES_PER_DAY=5

Trading-day boundaries use:
America/New_York

not the server's local timezone.
14. Closed-Market PRELIVE Test
Set:
WEBHOOK_TEST_MODE=false
PRELIVE_DRY_RUN=true

Send a LIVE-shaped signal.
Expected event path:
SIGNAL_RECEIVED
RISK_APPROVED
SIGNAL_QUEUED
WORKER_CLAIMED
EXECUTION_FRESHNESS_APPROVED
SAFETY_APPROVED
MARKET_SESSION_CLOSED
MARKET_CLOSED_BLOCK

Expected final state:
BLOCKED

There must be no broker order.
15. Open-Market PRELIVE Test
This test is mandatory before enabling real LIVE trading.
Configuration:
LIVE_TRADING=true
WEBHOOK_TEST_MODE=false
PRELIVE_DRY_RUN=true

Expected path:
SIGNAL_RECEIVED
RISK_APPROVED
SIGNAL_QUEUED
WORKER_CLAIMED
EXECUTION_FRESHNESS_APPROVED
SAFETY_APPROVED
MARKET_SESSION_OPEN
PRELIVE_DRY_RUN_PASSED
PRELIVE_DRY_RUN_BLOCK

These events must NOT occur:
ORDER_IDS_ALLOCATED
ORDER_SUBMISSION_INTENT
IBKR_PLACE_ORDER_RETURNED

Check the signal:
sqlite3 ~/trading-bot/trading.db "
SELECT
    signal_id,
    test_mode,
    status,
    parent_order_id,
    entry_order_id,
    target_order_id,
    stop_order_id
FROM signals
WHERE signal_id='SIGNAL_ID';
"

Expected:
test_mode       = 0
status          = BLOCKED

parent_order_id = NULL
entry_order_id  = NULL
target_order_id = NULL
stop_order_id   = NULL

If an order ID is present, stop testing.
Do not enable LIVE.
16. Query Recent Signals
sqlite3 ~/trading-bot/trading.db "
SELECT
    signal_id,
    test_mode,
    symbol,
    status,
    parent_order_id,
    updated_at
FROM signals
ORDER BY created_at DESC
LIMIT 20;
"

17. Query Active Signals
sqlite3 ~/trading-bot/trading.db "
SELECT
    signal_id,
    symbol,
    status,
    parent_order_id,
    updated_at
FROM signals
WHERE status IN (
    'QUEUED',
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
ORDER BY updated_at;
"

Before enabling LIVE, no unexpected unresolved signals should exist.
18. Query Events
Recent events:
sqlite3 ~/trading-bot/trading.db "
SELECT
    event_id,
    signal_id,
    source,
    event_type,
    old_status,
    new_status,
    message,
    created_at
FROM trade_events
ORDER BY event_id DESC
LIMIT 50;
"

One signal:
sqlite3 ~/trading-bot/trading.db "
SELECT
    event_id,
    event_type,
    old_status,
    new_status,
    message,
    created_at
FROM trade_events
WHERE signal_id='SIGNAL_ID'
ORDER BY event_id;
"

19. Query Executions
sqlite3 ~/trading-bot/trading.db "
SELECT
    exec_id,
    signal_id,
    order_id,
    perm_id,
    symbol,
    side,
    shares,
    price,
    exec_time
FROM trade_executions
ORDER BY received_at DESC
LIMIT 50;
"

20. Monitor Order Identity
The monitor uses:
orderRef
permId
orderId
broker parentId

Never assume:
TP = parent order ID + 1
SL = parent order ID + 2

Completed orders must not be stored solely by orderId because Interactive Brokers may return multiple completed orders with:
orderId=0

21. Partial Fills
TradingMax may observe:
partial ENTRY
partial TAKE PROFIT
partial STOP LOSS

A partially closed trade remains:
OPEN_POSITION

until the full entry quantity is closed.
22. Double Exit
If both TP and SL executions are observed, the monitor should record:
DOUBLE_EXIT_EXECUTION

and transition the signal to:
ERROR

This requires immediate broker-state inspection.
23. Cancellation
TradingMax cancellation endpoint:
POST /cancel/{signal_id}

Typical lifecycle:
CANCEL_REQUESTED
    ->
CANCELLING
    ->
CANCEL_PENDING
    ->
CANCELLED

If cancellation cannot be confirmed:
CANCEL_UNKNOWN

requires manual inspection.
Do not use a broker-wide global cancel as a normal recovery method.
24. Kill Switch
The kill switch prevents new LIVE trading.
Use it if:
broker behavior is unexpected
broker state is inconsistent
unexpected fills occur
orders cannot be reconciled
TWS state becomes unreliable
software behavior is suspicious

After enabling the kill switch, inspect TWS and TradingMax state before doing anything else.
25. Database
Main database:
~/trading-bot/trading.db

Important tables:
signals
trade_events
trade_executions
runtime_status
runtime_components
system_control

Never delete trading.db as a troubleshooting shortcut.
26. Database Backup
Backup timer:
trading-db-backup.timer

Check:
systemctl status \
  trading-db-backup.timer \
  --no-pager

Scheduled timers:
systemctl list-timers --all \
  | grep -i trading

Manual backup:
sudo systemctl start \
  trading-db-backup.service

Check:
systemctl status \
  trading-db-backup.service \
  --no-pager

List backup files:
find ~/trading-bot/backups \
  -maxdepth 1 \
  -type f \
  -printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n' \
  | sort

A valid backup must:
exist
have a recent timestamp
have a non-zero size

27. Python Validation Before Restart
Enter the virtual environment:
cd ~/trading-bot
source venv/bin/activate

Compile changed files before restarting services.
Examples:
python -m py_compile signal_server.py
python -m py_compile worker.py
python -m py_compile trade_monitor.py
python -m py_compile status_collector.py
python -m py_compile watchdog.py
python -m py_compile execution_guard.py
python -m py_compile position_policy.py
python -m py_compile system_report.py

No output means compilation succeeded.
Do not restart a critical service if compilation fails.
28. Safe Deployment Procedure
For a critical code change:
1. Edit the complete file
2. Run py_compile
3. Review git diff
4. Restart only the affected service
5. Check service status
6. Check journal logs
7. Run system_report.py
8. Run the relevant regression test
9. Commit
10. Push

29. Git Workflow
Check status:
cd ~/trading-bot
git status

Review:
git diff

Stage specific files:
git add FILE1 FILE2

Commit:
git commit -m "Description"

Push:
git push

Before important LIVE transitions:
git tag prelive-ready-YYYY-MM-DD
git push origin prelive-ready-YYYY-MM-DD

30. Files That Must Never Be Committed
Never commit:
.env
venv/
.venv/
__pycache__/
trading.db
*.db
*.db-wal
*.db-shm
backups/
credentials/
secrets/
*.pem
*.key
*.p12
*.pfx

Check:
git status

before every commit.
31. Worker Startup Recovery
After restart, the worker must reconcile with IBKR before it is considered ready.
Expected log flow:
STARTUP | waiting for successful IBKR reconciliation

then:
RECONCILE ...

then:
STARTUP READY | IBKR reconciliation completed

Do not rely only on:
systemctl is-active trading-worker

The service may be active while reconciliation has not completed.
32. Monitor Startup
Expected:
TradingMax trade monitor started
clientId=40
event_store=enabled
identity=orderRef -> permId -> orderId -> broker parentId
completed_order_storage=list

When no active LIVE signals exist:
MONITOR | no active signals

is normal.
33. First LIVE Trade Checklist
Before the first LIVE trade:
[ ] Git working tree clean
[ ] Latest code pushed
[ ] Database backup verified
[ ] TWS connected
[ ] Correct IB account
[ ] All services active
[ ] Watchdog healthy
[ ] Broker snapshot fresh
[ ] Daily P/L available
[ ] Net liquidation available
[ ] Available funds valid
[ ] Managed positions below limit
[ ] Total positions below emergency limit
[ ] No unexpected open orders
[ ] No unexpected active signals
[ ] Kill switch OFF
[ ] Market open
[ ] PRELIVE test passed
[ ] PRELIVE_DRY_RUN_PASSED observed
[ ] PRELIVE_DRY_RUN_BLOCK observed
[ ] No order IDs allocated in PRELIVE
[ ] No order submission event in PRELIVE
[ ] No order appeared in TWS during PRELIVE

Only after all checks pass should:
PRELIVE_DRY_RUN=false

be enabled.
34. First LIVE Trade Procedure
Open worker logs:
sudo journalctl -fu trading-worker

Open monitor logs in another terminal:
sudo journalctl -fu trading-monitor

Keep TWS visible.
Send one controlled signal only.
Verify:
signal accepted
signal claimed once
freshness approved
safety approved
market approved
order IDs allocated once
one bracket submitted
entry visible in TWS
TP visible in TWS
SL visible in TWS
orderRef values correct
monitor resolves identity
executions recorded
position state correct
exit state correct

Do not send another signal until the first complete lifecycle has been validated.
35. Emergency Procedure
If unexpected trading behavior occurs:
Step 1
Enable the TradingMax kill switch.
Step 2
Do not delete the database.
Step 3
Inspect Interactive Brokers TWS directly.
Check:
positions
open orders
executions
completed orders

Step 4
Run:
python ~/trading-bot/system_report.py

Step 5
Inspect worker logs:
sudo journalctl \
  -u trading-worker \
  --since "30 minutes ago" \
  --no-pager

Step 6
Inspect monitor logs:
sudo journalctl \
  -u trading-monitor \
  --since "30 minutes ago" \
  --no-pager

Step 7
Inspect the affected signal and its events in SQLite.
Do not use account-wide global cancellation unless explicitly required by the emergency situation.
36. Safe Recovery Principle
TradingMax follows:
No broker data
    ->
No trade

Stale broker data
    ->
No trade

Wrong account
    ->
No trade

Unknown order identity
    ->
No assumption

Risk failure
    ->
No trade

Market closed
    ->
No trade

Kill switch ON
    ->
No trade

Ambiguous state
    ->
Block and investigate

Missing a trading opportunity is preferable to creating an uncontrolled broker order.



37. Micro-cap Rebound Historical Research (research only)
`microcap_research.py` produces a read-only JSON research report for one symbol. It has zero order side effects: it never connects to IBKR or any broker, never calls an order endpoint, and never reads or writes TradingMax state. Its `RESEARCH_ELIGIBLE` decision is not an order approval and there is no `BUY_ELIGIBLE` output in this phase; IBKR live data and paper execution are later, separate phases.

Data source:
Alpaca Market Data API only (`https://data.alpaca.markets`): `/v2/stocks/bars` (1Min, 1Hour, 1Day; split-adjusted, plus raw daily for a price-basis check), `/v2/stocks/quotes` (historical bid/ask) and `/v1beta1/news`.
This is not the Alpaca paper trading endpoint (`paper-api.alpaca.markets`); nothing is sent to a trading API.

Credentials (variable names only, never values):
APCA_API_KEY_ID
APCA_API_SECRET_KEY

Configure them privately in your own shell session, never in the repository, `.env` files committed to git, command arguments, tickets, chat, or logs. If keys were ever pasted anywhere shared, rotate them in the Alpaca dashboard first and use only the new pair. Example, typing values at hidden prompts:
read -rs APCA_API_KEY_ID && export APCA_API_KEY_ID
read -rs APCA_API_SECRET_KEY && export APCA_API_SECRET_KEY

The tool reads only these two variables, sends them only as Alpaca request headers, and never prints, logs or saves them. Unset them when finished:
unset APCA_API_KEY_ID APCA_API_SECRET_KEY

Exact invocation:
cd /home/oferke/trading-bot
venv/bin/python microcap_research.py --symbol ACME --company "Acme Corp" --as-of 2024-05-15T14:31:00Z --start 2023-01-03T00:00:00Z --end 2024-05-15T14:31:00Z --feed iex

Arguments:
--as-of, --start and --end must be RFC-3339 timestamps with a timezone (UTC `Z` recommended).
History is [start, end) and end must not be after as-of; nothing after as-of is requested.
--feed accepts only the free `iex` feed.
--fees-per-share is a predeclared round-trip fee per share (default 0.02 USD). Set it higher for small share counts where per-order minimum commissions dominate.

Exit codes:
0 = JSON report on stdout (`NO_TRADE` or `RESEARCH_ELIGIBLE`)
2 = invalid input (bad timestamps, range, feed, fees); no API call is made
3 = credentials missing or Alpaca API/data error (including `QUOTE_COVERAGE_UNAVAILABLE` when historical quotes are not entitled)
Errors are a single JSON line on stderr without credentials.

Free-tier (Basic plan) limitations:
IEX feed only: IEX bars miss trades on other venues, so missing minutes may simply mean no IEX trade. IEX quotes are IEX top-of-book, not the consolidated NBBO, so spreads/fills differ from IBKR.
Historical calls are limited to 200/min; quote windows are requested around each candidate decision with a pause between calls.
SIP data within the latest 15 minutes is unavailable; the tool does not use it.
Alpaca news has no completeness guarantee. Missing articles are reported as `NEWS_COVERAGE_UNVERIFIED`, never as proof that no catalyst existed.
Quotes are raw (unadjusted); bars are split-adjusted. Estimates require raw and split-adjusted daily bars to match over the window, otherwise `PRICE_BASIS_MISMATCH`/`PRICE_BASIS_UNVERIFIED`.

How it decides:
Fixed parameter grid (`MODEL_VERSION` in the report); primary-news articles for the same symbol whose 48-hour windows overlap are merged into one independent catalyst episode. All catalyst days and events associated with an episode stay together in a chronological 60% train / 20% validation / 20% holdout split. Parameters are chosen on train episodes only, confirmed on validation episodes, and the holdout is evaluated afterwards for metrics, baseline comparison (hold-to-horizon and no-trade), drawdown and calibration. The report preserves `catalyst_days` and separately reports `independent_episodes`; event metrics use at most one event per episode.
At least 50 independent train-plus-validation episodes are required before target probabilities can be estimated, and each split needs at least 10 independent event episodes. With the fixed split fractions, at least 64 total independent episodes are needed to meet those requirements. Fewer independent episodes, missing quotes/news/bars, unverified price basis, a stale as-of quote, a live same-day as-of (`LIVE_IBKR_STATE_REQUIRED`), no setup, insufficient in-bucket episodes, or a non-positive validation/holdout result all produce `NO_TRADE` with reasons. Unsupported target probabilities are reported as `unavailable` with a reason, never estimated.
A same-day as-of needs IBKR live state, which is not part of this phase; delayed Alpaca data is never substituted for it.
