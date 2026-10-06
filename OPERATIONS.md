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

38. Micro-cap Coverage Inventory (Task 4; read-only)
`microcap_coverage_probe.py` inventories a **predeclared**, bounded issuer/date sample. It never calls broker account or order APIs, never calibrates a model and never approves an order. Inspect its actual options with:
`/home/oferke/trading-bot/venv/bin/python microcap_coverage_probe.py --help`

Inputs must be absolute paths to external JSON files (outside the repository). A manifest contains `{"status":"predeclared","issuer_ids":["issuer-a"],"dates":["2024-05-15"]}`. The roster contains dated member records with `issuer_id`, `symbol`, `company`, `valid_from`, `valid_to`, `listing_status`, and `source_record_id`, plus `source_url`, `retrieved_at`, and `coverage_claim`. The default `microcap_roster.eligible_members` requires `coverage_claim: "historical-listed-and-delisted"`; only an explicit `--pilot` accepts `coverage_claim: "pilot-unverified"` with the same provenance, member identity, unique record, and nonoverlapping date-window checks. A pilot roster without `--pilot` fails with `ROSTER_UNVERIFIED`, including offline mode. If one issuer has more than one symbol active on a predeclared date, the run exits 2 with `CONTRACT_UNRESOLVED` before any provider request; the probe never picks a ticker by input order. These *claims* and self-supplied fields are **not independent historical evidence** of listed AND delisted coverage. The current roster-source investigation is `ROSTER_UNVERIFIED`: offline reports always say so; non-pilot fetch refuses it. A pilot may run on an unverified roster, but is permanently labeled `UNVERIFIED_PILOT`, biased, and `NO_TRADE`. Source provenance and independent dated roster validation must be resolved before any non-pilot research claim. Do not substitute today's top gainers for a dated roster.

Example **unverified pilot input only** (dummy ticker/issuer and placeholder URL, not independent source proof; save externally as `/external/pilot-roster.json` and predeclare the matching issuer/date in the manifest):
```
{
  "source_url": "https://example.com/pilot-roster",
  "retrieved_at": "2024-05-16T00:00:00Z",
  "coverage_claim": "pilot-unverified",
  "members": [{
    "issuer_id": "dummy-issuer", "symbol": "DUMMY", "company": "Dummy Example",
    "valid_from": "2024-05-01T00:00:00Z", "valid_to": null,
    "listing_status": "listed", "source_record_id": "dummy-record"
  }]
}
```
Do not use this dummy ticker for a real provider request; replace it with an independently selected, predeclared test symbol before a read-only pilot, without asserting historical roster completeness.

Offline replay (default; zero connections, even if provider credentials exist):
```
/home/oferke/trading-bot/venv/bin/python microcap_coverage_probe.py --roster /external/dated-roster.json --sample-manifest /external/predeclared-sample.json --observations /external/observations.json --start 2024-05-15T00:00:00Z --end 2024-05-16T00:00:00Z
```
The observations file is an array (or `{"observations":[...]}`) of issuer/date/session/provider cells with `bars`, `quotes`, `news` channel status objects. For actual read-only probing after manually checking that the dedicated broker ID is not in use by any service:
```
/home/oferke/trading-bot/venv/bin/python microcap_coverage_probe.py --roster /external/pilot-roster.json --sample-manifest /external/predeclared-sample.json --start 2024-05-15T00:00:00Z --end 2024-05-16T00:00:00Z --pilot --fetch --max-symbols 1 --max-days 1
```
Only `--fetch` enables connections; `--pilot` never makes observations eligible. Fetch probes IBKR only by default. Add `--marketaux` to explicitly request optional Marketaux news; that mode requires `MARKETAUX_API_TOKEN`, and a requested-provider error exits 3. Never pass token values as arguments or paste them into commands/reports. Prepare IBKR environment variables privately: `IB_PROBE_CLIENT_ID` (dedicated ID >=100, not used by other bots), `IB_PROBE_HOST` (optional, defaults to localhost), and `IB_PROBE_PORT` (optional, defaults to 7497). Both `--max-symbols` (1..10) and `--max-days` (1..7) are mandatory on fetch; `--max-symbols` limits distinct resolved tickers across dates, so a ticker change for one issuer counts as another symbol. The UTC window is exclusive at end and cannot exceed the day limit or IBKR's seven-day request limit. Manifest types, unique issuer/date values, date boundaries, and resolved ticker limits are checked before provider requests. Contract lookup, historical bars, historical bid/ask ticks, and historical news are the only IBKR requests. Historical bars use `formatDate=2`: 1-minute and 1-hour `BarData.date` values must be epoch seconds and are normalized to UTC (anything else is an explicit `TIMESTAMP_INVALID` error, aggregated per request with a count and at most three raw examples). Daily bars are date-only exchange session labels, reported separately under `daily_bars` with `timestamp_basis: "SESSION_DATE_ONLY"` and `DAILY_BAR_DECISION_TIME_UNVERIFIED`; no intraday availability instant is inferred, and they never count toward the `bars` channel. The coverage report keeps intraday bars as metadata only: `matrix[].bars.intervals` gives separate `1 min`/`1 hour` counts with first/last UTC (no prices, volumes or per-bar records), and `matrix[].daily_bars` gives the daily count and first/last session date with `SESSION_DATE_ONLY`, `decision_time_verified: false` and `intraday_gate_eligible: false`. Only 1-minute bars satisfy bar coverage: a positive combined or hourly-only count adds `MINUTE_BAR_COVERAGE_UNAVAILABLE` (plus `MINUTE_BARS_MISSING_HOURLY_ONLY` when hourly bars exist), and bars without interval metadata add `BAR_INTERVAL_UNVERIFIED`. Top-level `bar_interval_coverage` reports observed cells and missing fractions per `1 min`/`1 hour`/`1 day`; daily coverage is diagnostic only and never fills an intraday gap. The decision is always `NO_TRADE`; any later trade path must stay blocked on data quality without 1-minute coverage. When enabled, the Marketaux read timeout is **best-effort**, not an end-to-end duration bound (DNS and slow body reads can overrun).

Entitlements are **UNVERIFIED**: no real IBKR or Marketaux requests have been run here. IBKR news request-window timezone interpretation is unresolved; timezone-naive article strings are ambiguous, never assumed UTC. Empty Marketaux results do not prove absence of catalysts. Provider errors, missing bars or bid/ask, delisted contract failures, and ambiguous timestamps remain reasons to stop, not negative observations. Every declared issuer/date requires an IBKR cell with observed bars and quotes; Marketaux-only rows cannot fill it. `coverage_status: "PROBE_COVERAGE_OBSERVED"` means only that coverage cells were observed; it is not a verified roster, a calibrated model, or trading eligibility. The decision is always `NO_TRADE` with `RESEARCH_ONLY_NOT_CALIBRATED`; `order_approval` and `model_calibrated` are false, and `target_probabilities` is `"unavailable"`. Exit 0 means a valid JSON inventory (including `NO_TRADE`), 2 means invalid input or unverified non-pilot fetch, and 3 means provider/entitlement failure with JSON stdout and one redacted JSON stderr line. Errors print one JSON object on stderr and nothing on stdout. If `MARKETAUX_API_TOKEN` would appear anywhere in the serialized output (for example a token equal to a fixed field such as `decision` or `error`), the run exits 2 as invalid credential input and the stderr diagnostic falls back through fixed alternatives (`{"error":"CREDENTIAL_CONFLICT"}`, `{"failure":"INVALID_INPUT"}`, `{"fail":2}`) to one that does not contain the token. A token consisting only of JSON punctuation present in every object (`{`, `}`, `"`, `:`, `{"`, `":`) cannot be reported safely: the run exits 2 with empty stdout and stderr.

39. Dated source feasibility extension (research only)
`microcap_source_probe.py` extends, but does not change, an existing `microcap_coverage_probe.coverage_report` JSON report. Check the actual interface:
```bash
./venv/bin/python microcap_source_probe.py --help
```
The predeclared manifest must contain exactly one issuer ID, one ISO date, and an uppercase `symbol` (for example SORA). Preserve the original coverage report's fields; add `sample_manifest.symbol` matching that manifest and `request_window: {"start_utc":"2025-05-28T00:00:00Z","end_utc":"2025-05-29T00:00:00Z"}` before saving the base report externally. The half-open UTC window must lie inside that one predeclared date. This metadata is necessary because the original coverage report does not itself record its request window or symbol. All input JSON paths must be absolute and external to the repository. A self-declared verified roster in the base report does **not** independently establish universe coverage. No orders, scanner, IBKR connection or probability calibration occur in this extension.

Offline replay (default; no network, even if provider credentials are configured):
```bash
./venv/bin/python microcap_source_probe.py --base-report /external/base-report.json --sample-manifest /external/predeclared-sample.json --start 2025-05-28T00:00:00Z --end 2025-05-29T00:00:00Z --roster-evidence /external/massive-snapshots.json --news-evidence /external/alpaca-news.json
```
Optional `--cap-evidence /external/filed-shares.json` cannot verify itself; without an independently audited, availability-time-qualified shares source the output **always** contains `MARKET_CAP_UNVERIFIED`. Offline Massive evidence is an object with separate `active` and `inactive` snapshots in `MassiveRoster.fetch_snapshot` format. Saved JSON is untrusted: it cannot assert observed pagination, dated coverage, or provider completeness, even when it contains `page_count` and `pagination_complete`. Inactive results are controls, not proof they were active on the historical date. News evidence uses `observed_news` format; zero articles mean `NEWS_COVERAGE_UNVERIFIED`, never confirmed absence. Only aggregate counts, not full article text or provider URLs, enter the report.

The extended report adds `massive_roster`, `alpaca_news`, `market_cap_gate`, and `blocking_reasons` to each existing IBKR `matrix[]` row; the original bar interval and daily-bar details are retained. A live Massive response reports actual `page_count` and `pagination_complete` separately for active and inactive queries, plus the dated filter response and unresolved identity count across both result lists. Completed pagination only means the bounded ticker-filter query ended; provider-wide roster completeness remains `UNVERIFIED_BY_PROVIDER` and `ROSTER_COVERAGE_UNVERIFIED` always blocks. Saved evidence reports unknown pagination/count fields rather than trusting self-declared metadata. Alpaca article counts are observations only, its historical completeness remains unverified, and the cap gate remains blocked absent audited filed-share evidence. Missing or malformed inputs have explicit source status and blocking reasons in both each matrix row and the report-level `reasons`.

Opt-in capped pilot, using only privately supplied environment variables `MASSIVE_API_KEY`, `APCA_API_KEY_ID`, and `APCA_API_SECRET_KEY` (never pass their values in arguments or saved JSON):
```bash
./venv/bin/python microcap_source_probe.py --base-report /external/base-report.json --sample-manifest /external/predeclared-sample.json --start 2025-05-28T00:00:00Z --end 2025-05-29T00:00:00Z --fetch --max-pages 2
```
`--fetch` requests Massive dated `active=true` **and** `active=false` for the one predeclared ticker/date, and Alpaca **news only** through `AlpacaHistory.from_environment()`. It never calls IBKR. `--max-pages` (1 or 2) caps both each Massive filter and the Alpaca news request. A continuing Massive `next_url` or Alpaca `next_page_token` beyond the cap fails closed (news: exit 3, `NEWS_PAGE_CAP_EXCEEDED`), never as a partial result. Massive Basic documents a **two-year historical roster limit**; older dates, ticker reuse, identifier continuity and full listed/delisted universe coverage remain unverified. A bounded investigation observed one Massive dated roster/date response and Alpaca SORA news on **two** requested windows (one article on 2026-10-01 and two on 2025-05-28); these samples do not prove provider archive completeness, issuer identity, event timing or any price catalyst. Marketaux independently returned Cloudflare HTTP 403; denial is **not** evidence of no news. Massive dated ticker-details market cap or shares can reflect later filings and must not be used to establish historical market cap. Filed-and-available-by-decision shares, complete share classes, matched raw price and corporate-action basis are still missing. The result is always `NO_TRADE`, with false order/model flags and unavailable target probabilities. Exit 0 reports valid `NO_TRADE` JSON; invalid input exits 2, provider failures exit 3 with a sanitized JSON error on stderr.

40. SEC filed-shares feasibility probe (research only)
Check the exact command-line interface:
```bash
./venv/bin/python microcap_sec_probe.py --help
```
The default is **offline**: provide two saved SEC JSON objects at absolute paths outside this worktree and a decision instant in UTC. This command constructs no SEC reader and makes no HTTP requests:
```bash
./venv/bin/python microcap_sec_probe.py --cik 0000123456 --decision-at 2025-05-28T15:00:00Z --submissions /external/submissions.json --companyfacts /external/companyfacts.json --output /external/sec-feasibility.json
```
`--output` is optional offline. Only `--fetch` enables network access; it cannot be combined with either saved input and requires an absolute external `--output` path. Set `SEC_USER_AGENT` privately to an identifiable SEC-compliant contact string before fetching; never put it on the command line or in reports:
```bash
./venv/bin/python microcap_sec_probe.py --cik 0000123456 --decision-at 2025-05-28T15:00:00Z --fetch --output /external/sec-feasibility.json
```
Fetch calls `SecReader.fetch` once: at most one `https://data.sec.gov/submissions/CIK0000123456.json` GET and one `https://data.sec.gov/api/xbrl/companyfacts/CIK0000123456.json` GET, with no retries, redirects or other endpoints. Inputs (ASCII CIK, UTC date, external paths and incompatible options) are validated before any provider request. The reader enforces per-response byte caps (2 MiB submissions, 16 MiB companyfacts) and socket timeout plus a **best-effort**, not hard end-to-end, elapsed check between reads; DNS, a blocking read or slow trickle may exceed the nominal timeout. Offline saved inputs use the same per-document caps. JSON saved with `--output` is written via same-directory temporary file and atomic replace, with failed temporary writes cleaned up. Never save raw SEC responses inside the repository.

Only the bounded `observe_shares` result enters the report (`market_cap_gate`): accession, SEC acceptance instant, filing/report dates, observed share count where safe, and explicit blockers. Acceptance must be no later than `--decision-at`; a company-facts `filed` *date* alone is not evidence of intraday availability. As a cross-check, `filed` may match the Eastern acceptance date, or the next business day for EDGAR acceptances at/after 17:30 America/New_York (weekends skipped; no holiday calendar). Only matched supported 10-K, 10-Q, 8-K and foreign-private-issuer 20-F filings can yield observed counts (20-F counts are annual, as of fiscal year end, so they can be many months stale relative to later 6-K or offering activity). Issuer identity comes from the CIK on both SEC documents; the accession prefix identifies the submitter (often a filing agent) and is not an issuer check. Amendments, unsupported forms (including 6-K), mismatched issuer/CIK, invalid dates/facts, ambiguous classes, future acceptance and truncated accession coverage block or suppress counts. Even a reported count does **not** establish completeness across share classes or independently verify the source, raw historical price, corporate actions, or decision-time market cap. `MARKET_CAP_UNVERIFIED` and `CLASS_COVERAGE_UNVERIFIED` remain; no orders, scanner, market-cap pass, calibrated model or target probabilities are produced. The decision is always `NO_TRADE`, `order_approval` and `model_calibrated` false, and `target_probabilities` `"unavailable"`.

Exit 0 means a valid **no-trade** JSON report on stdout (and optionally at the external output path); invalid input exits 2, provider 403/timeout exits 3 with `SEC_ACCESS_UNAVAILABLE`, oversized live responses exit 3 with `SEC_RESPONSE_TOO_LARGE`, and 429 exits 3 with `SEC_RATE_LIMITED`. Errors have sanitized JSON on stderr and no success stdout. SEC live access has **not been verified by this offline implementation/test run**; if a real provider request fails, record access as unavailable, **not** as evidence that there are no filings. Do not infer actual filing absence from an empty or inaccessible response.

## Micro-cap Research Readiness Panel

Publish the research-only readiness snapshot from external evidence, never from the dashboard UI:

```bash
/home/oferke/trading-bot/venv/bin/python microcap_readiness.py \
  --source-report /external/source.json \
  --output /external/readiness.json \
  [--sec-report /external/sec.json]
```

Configure the signal server with `MICROCAP_READINESS_PATH=/external/readiness.json` using an absolute path outside the repository. The server treats snapshots older than seven days as `STALE`; missing, invalid, forged, or unreadable snapshots render as `UNAVAILABLE`.

`/dashboard` is the focused micro-cap dashboard. The top bar holds the PAPER/LIVE badge, the NO TRADE research badge, the IBKR connection dot (grey when the broker snapshot is missing or older than 60 s) and the kill switch. Sidebar tabs: Research (schema 2 batch coverage KPIs and the sample table, or the schema 1 single-sample view), Positions, Orders, and Health (runtime components, safety blockers, PAPER/LIVE switch with double confirmation for LIVE). The selected tab is kept in the URL hash (`#research`, `#positions`, `#orders`, `#health`). The previous full dashboard remains at `/dashboard-legacy`. Scripts are served by the authenticated `/microcap-research-panel.js` and `/dashboard-app.js` routes. Removed sections keep their backend routes.

## Micro-cap Batch Coverage Pilot

Research only. Measures how often IBKR, Massive, Alpaca and SEC cover a mechanically selected batch of recent runner days. Every output is `NO_TRADE`. The selection uses the day's high (`SELECTION_USES_SAME_DAY_OUTCOME`), so these samples must never be used to calibrate or evaluate entry rules.

All paths are absolute and outside the repository. Load the private environment without printing it (`set -a; . ~/.config/alpaca/microcap.env; . ~/.config/tradingmax/sec.env; set +a`, plus `MASSIVE_API_KEY` and `IB_PROBE_CLIENT_ID=177`).

1. Select (Massive grouped daily and ticker type, SEC tickers; at most 5 Massive calls per minute):

   ```bash
   ./venv/bin/python microcap_batch_select.py --days 10 --per-day 2 --output /external/batch/batch-manifest.json
   ```

   Exit codes: 0 = ok; 2 = invalid input or `ENVIRONMENT_INCOMPLETE`; 3 = `PROVIDER_ERROR` or `NO_SAMPLES_SELECTED`.

2. Create the output directory first (`mkdir -m 700 /external/batch/runs`; the runner refuses a missing directory with `INPUT_INVALID`). Then run every sample through the existing IBKR, source and SEC CLIs. This takes about 25 s or more per sample. TWS paper on port 7497 must be up.

   ```bash
   ./venv/bin/python microcap_batch_run.py --manifest /external/batch/batch-manifest.json --out-dir /external/batch/runs [--resume]
   ```

   Each sample gets `<out-dir>/<date>-<cik>/` holding the stage inputs and outputs and `sample-result.json`. `--resume` reuses valid results. A failing stage is recorded as `<stage>:<PROVIDER_ERROR|INVALID|TIMEOUT|SKIPPED>` and never aborts the batch.

3. Publish a schema 2 snapshot for the dashboard:

   ```bash
   ./venv/bin/python microcap_readiness.py --batch-report /external/batch/runs/batch-report.json --output "$MICROCAP_READINESS_PATH"
   ```

   `--batch-report` and `--source-report` are mutually exclusive. `--sec-report` applies only to `--source-report`.
