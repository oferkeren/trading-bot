# As-of shares feasibility and research readiness dashboard

## Goal and boundaries

Continue the micro-cap research phase by testing whether publicly available SEC
filings can supply defensible as-of outstanding shares, and show the existing
research gate's progress in the TradingMax dashboard. This is **not** the
IBKR live evaluator or paper controller. No order, scanner, strategy-mode or
trade-approval path may consume these observations. Every published research
status says `NO_TRADE`; `order_approval` and `model_calibrated` stay false, and
target probabilities stay unavailable.

The current `microcap_source_probe.py` report remains the source of roster,
news, market-cap and IBKR coverage observations. Its saved inputs are not
independent proof of a historical universe or an audited shares series.
Massive dated ticker-details market cap and shares are inadmissible. Existing
live-bot/dashboard files in the main checkout contain unrelated uncommitted
changes; implementation uses an isolated worktree and must not overwrite them.

## SEC shares feasibility

Create a separate, read-only SEC pilot, gated by an external predeclared
sample of one issuer (stable CIK) and one historical decision window. Offline
replay is the default. Explicit fetch uses public SEC data only, a configured
identifying User-Agent, bounded requests/filings, timeouts and response sizes,
and a private, out-of-repository evidence location. Network refusal or
entitlement/access failure is an error/unavailable reason, never zero shares.
Do not send previously exposed provider credentials or licensed roster/news
data to SEC. If this environment still cannot access SEC, record the failure
and stop the live pilot; offline tests must remain independent of that access.

For each candidate filing, distinguish report-period end, filing acceptance
time (public availability), observation/fetch time, accession/amendment and
security class. Keep the issuer/CIK match, filing URL/provenance and whether
the count describes outstanding shares rather than authorized or weighted
shares. A filing with `accepted_at > decision_at` cannot justify the
historical decision even if its reporting period predates it. Ambiguous
acceptance time, multiple or unknown share classes, missing corporate-action
history, stale or mismatched raw prices, ticker reuse and incomplete filing
coverage all block classification. A filing response alone cannot set
`source_verified=True` in `classify_microcap`: an independently reviewed
source/identity/class/availability protocol is required. This pilot reports
feasibility and blockers, not a calibrated issuer-cap series. No price source
is added by this work.

## Saved status and dashboard

An offline publisher validates an existing source report and optional SEC
pilot evidence, reduces them to an allowlisted aggregate status, and writes
one JSON snapshot atomically outside the repository. The snapshot includes
schema version, report creation time, declared issuer/date/window, `NO_TRADE`,
source status/counts, SEC feasibility status, cap-gate status and sorted
blocking reasons. It excludes keys, provider URLs, raw filings, article text
and prices; it cannot accept external `order_approval`, calibration or source
verification claims as authorization. Invalid report/evidence fails the
publish operation rather than replacing a valid snapshot with success-shaped
data.

An authenticated read-only endpoint in the existing dashboard server reads
only that configured absolute status path. It never runs a probe, imports
broker/order workers, or follows client-supplied file paths. Missing,
malformed, unsupported-version, or stale status returns a fixed, sanitized
`RESEARCH STATUS UNAVAILABLE/STALE` with `NO_TRADE` and blockers. Staleness
is based on the saved report's creation time, not the historical sample date;
the UI displays the time and a clear age label, not a misleading live badge.
The dashboard has a dedicated panel above the scanner, separate from live
strategy controls, showing roster, news, IBKR minute/quote coverage, SEC
shares/cap status, and blocking reasons. No completion percentage, trading
signal or actionable buy/sell control appears.

## Verification and handoff

Use mocked SEC responses for acceptance-time boundaries, amendments,
share-class ambiguity, wrong issuer, invalid/missing metadata, truncation and
access errors. Test that a positive filing cannot by itself clear
`MARKET_CAP_UNVERIFIED`. Test publisher schema/redaction/atomic write and
missing/stale/invalid snapshot behavior, authenticated endpoint isolation and
dashboard rendering with the existing UI conventions. Existing offline
micro-cap suites remain passing. A bounded real SEC pilot is optional only
after access and identification policy are verified; record observed data or
an access blocker, never infer broad archive completeness from one sample.
Historical roster completeness, filed-share qualification, and independent
holdout calibration remain gates before the separate live evaluator or
paper execution phases.
