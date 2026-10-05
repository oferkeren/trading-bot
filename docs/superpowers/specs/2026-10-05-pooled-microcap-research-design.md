# Pooled micro-cap catalyst research redesign

## Purpose and boundary

This is a research-only continuation of the
[news-driven micro-cap rebound design](2026-10-04-microcap-rebound-paper-design.md).
The first, single-symbol historical research CLI ran against real Alpaca data for
SORA/AsiaStrategy. It returned `NO_TRADE`: historical bid/ask quotes were
unavailable, and the observed history could not establish enough independent
catalyst episodes for a per-symbol probability. This result does not validate a
strategy or establish that another provider has adequate coverage. The pooled
research design addresses the per-symbol sample problem without lowering the
independence or cost requirements.

No part of this project places, authorizes, or simulates broker orders. It does
not modify the existing live scanner or activate the IBKR live evaluator or
single-symbol paper controller. Those remain separate phases; paper activation
requires the original design's real-data validation and an additional explicit
decision. There is no live-trading activation in this project.

## Stage 1: coverage and historical universe

Run a bounded, read-only feasibility probe before implementing a pooled model.
Locate and verify a dated source for the historical micro-cap universe with
currently listed **and known delisted/failed** issuers, as-of symbol/issuer
identity and membership, source provenance, corporate actions, and coverage
periods. A present-day ticker list or hand-picked gainers is not an unbiased
historical universe. A pilot list may probe provider endpoints but cannot
support strategy-level performance claims.

On a predeclared representative sample including winners and failed/delisted
issuers, measure actual entitlement, earliest/latest available timestamps,
session and venue coverage, timestamps/adjustments, missing minutes, and
request-rate behavior for:

- IBKR historical 1-minute, hourly, and daily trades/bars, plus bid/ask
  history suitable for post-decision fill and stop-cost modeling.
- IBKR historical news, including provider permissions, issuer mapping,
  article IDs, publication times, depth, and completeness limitations.
- Marketaux Free news as an optional second provider, including actual quota,
  historical depth, timestamp precision, issuer mapping, and entitlement.

IBKR bars and quotes are the price source; **either** IBKR or Marketaux may
qualify a verified article. Marketaux is not a substitute for missing quotes.
No historical-depth, entitlement, free-plan, or delisted-contract claim is
assumed until the probe observes it. Keep provider credentials outside reports,
repository files, and command lines. Use bounded requests, explicit pacing and
resumable collection only if access is confirmed.

The probe produces a coverage matrix by issuer, date, session, and provider,
plus explicit reasons for unsupported dates, inaccessible contracts, missing
quotes, insufficient news coverage, and exhausted quotas. Missing articles
are **unknown coverage**, not proof of no catalyst. A provider error cannot be
turned into an empty, apparently valid historical sample. The stop/go decision
is whether enough point-in-time observations and a defensible dated universe
exist to test the predeclared episode and split gates below. If not, stop at a
`NO_TRADE` feasibility report and document prospective archiving of IBKR
quotes/scanner states and verified news as a future alternative; do not
manufacture probabilities or claim an unbiased backtest.

## Stage 2: point-in-time pooled replay

Only if Stage 1 passes, construct historical candidates by scanning each
session's **then-observable** price/volume conditions over the dated universe,
including symbols that later failed, rather than selecting past winners after
seeing their returns. Preserve issuer identity across ticker changes, delisting
and splits; reject unresolved price-basis or identity mismatches. Use existing
pure cycle/event/target primitives where their contracts fit, but replace the
Alpaca-specific single-symbol report orchestration with a multi-symbol study.
The pooled replay and providers have separate interfaces and no dependency on
the current live order path.

For each candidate, require a company-specific, novel, timestamped article
that was published by the decision instant and is at most 48 hours old; record
its age so a stricter 24-hour subset can be evaluated. Verify issuer/subject,
source, publication time and relevance, and deduplicate syndicated/recycled
coverage across providers. Require a predeclared, measurable **post-publication**
price and volume reaction that is still active at the decision; merely finding
an article near a move is insufficient. This is evidence of a plausible active
catalyst, not proof of causation. An article published after a move can support
only later decisions for which a subsequent reaction is observable, never
retroactively label earlier entries. Reject stale, wrong-company, ambiguous,
unverifiable or merely adjacent news and unavailable reaction baselines.

At each decision use only complete bars and quotes already known, a confirmed
low after the required completed cycles, and comparable session/liquidity
conditions. Model entry at the first executable post-decision ask, never the
signal bar's close. Account for fees, observed bid/ask spread and conservative
slippage; evaluate stops before targets on ambiguous bars and price gap-through
stops conservatively. Unsupported fills or incomplete outcome windows are not
successful trades. All targets around $0.50, $1.00, $1.50 and $2.00 per share
are *candidates*, not promised profits or automatically selected outcomes.

## Independence, selection and validation

Merge overlapping 48-hour news-eligibility windows for the **same issuer**
into one episode, including transitively overlapping articles and cross-source
duplicates of the same story. Associate each repeated setup/trade within the
active episode with that episode, and count the episode at most once for
independent confidence intervals and split eligibility. Repeated-trade
sequences remain a separate descriptive analysis of actual sequence risk and
net returns; their individual trades are not additional independent samples.
Report concentration by issuer and period and stress-test results against
issuer and market-day clustering. Do not claim independence merely because
symbols differ when a common market-wide story drives the setups.

Predeclare the candidate criteria, catalyst/reaction rule, feature buckets,
cost assumptions, parameter grid, independence key, and acceptance thresholds
before inspecting final holdout outcomes. Order whole episodes chronologically
into train, validation and an untouched holdout. Purge across split boundaries
through each episode's actual end (including chains of overlapping 48-hour
windows) plus the maximum outcome horizon; do not move an event whose decision
or outcome crosses the boundary into a different split. Freeze cycle parameters
and target-selection rules on train; confirm on
validation; report final holdout once after freezing. A symbol can appear in
more than one split only through distinct, separated episodes, and the report
must expose that concentration and its sensitivity.

Retain at least 50 independent, comparable train-plus-validation episodes for
each feature bucket receiving a probability, and at least 10 independent
episodes with complete outcomes in each of train, validation and holdout for
the chosen configuration. The original fixed 60/20/20 episode split implies a
minimum of 64 total episodes **before** additional purge, missing-data,
comparability and concentration losses. Merely reaching 64 raw articles or
pooled episodes does not pass these gates. No pooling across unrelated feature
buckets to force a probability; unsupported buckets remain `unavailable`.
Use conservative probability and mean-net confidence bounds; require the
predeclared conservative net and calibration gates to hold on validation and
holdout, after costs and compared with no-trade and a simple rebound baseline.
Report hit rate, probability calibration, target-before-stop timing, net
outcome, drawdown, sensitivity to fills, sample size and uncertainty. A failed
or unavailable holdout cannot yield an eligible frozen artifact.

## Research outputs and failure behavior

Publish provider/universe provenance and coverage, reproducible collection
window, frozen model/grid version, symbol/period breakdowns, episode and
comparable-bucket counts, split boundaries and purges, selection and holdout
metrics, and explicit `NO_TRADE` reasons. The output distinguishes data
availability, historical model eligibility and candidate eligibility;
`order_approval` is always `false`. Store a versioned model artifact only when
all real-data gates pass; otherwise report unavailable probabilities, not
defaults.

Preserve the intended single-stock use case as a research evaluation: one
candidate at a time, a fresh active company-specific catalyst, a statistically
favorable low, a realistic cost-adjusted rebound target, and repeat-suitability
that expires when catalyst strength, volatility, volume, spreads, or rebound
quality deteriorate. This is an evaluation contract for later work, **not** an
implementation of symbol ownership, account checks, or order execution.

Test with mocked IBKR and Marketaux responses and synthetic dated rosters
covering delisted/ticker-changed issuers, provider refusal or pagination,
incomplete quotes, stale or wrong-company articles, recycled/cross-source
stories, news published after a move, weak post-news reaction, extended-hours
gaps, incomplete fills/outcomes, sparse buckets, split purges, ambiguous
stop/target ordering, issuer concentration, and deteriorating repeat setups.
Offline tests verify mechanics, not calibration. Only an actual coverage probe
and chronological real-data holdout can support a validated research result.
