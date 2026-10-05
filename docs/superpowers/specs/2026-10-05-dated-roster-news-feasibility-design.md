# Dated micro-cap roster and news feasibility extension

## Context and boundary

This research-only extension follows the approved
[pooled micro-cap design](2026-10-05-pooled-microcap-research-design.md) and
the implemented read-only coverage gate. A bounded IBKR paper-TWS historical
probe observed 327 one-minute bars, six hourly bars, one date-only daily bar,
and 56 bid/ask observations for one SORA window. That demonstrates access for
one symbol/date, not broad historical coverage. IBKR's historical-news time
and request-window coverage remain unverified. Marketaux requests from this
environment returned a Cloudflare HTTP 403, which is a provider access
failure, **not** proof of absent news. Alpaca returned timestamped Benzinga
articles for two sampled SORA days, but guarantees no complete archive.

Massive's documented dated ticker endpoint returned SORA as active and other
symbols as inactive for a sampled historical date. This is a roster feasibility
observation, not an independently audited point-in-time universe. The Massive
Basic plan documents two years of dated roster history; measure actual
entitlement and rate limits rather than extrapolate from a single response.
Massive ticker-details documentation says a dated request may use an SEC
filing submitted **after** the requested date. Therefore its dated
`market_cap` or outstanding shares cannot be used as an as-of feature without
an independent availability-time check.

There are no broker orders, account snapshots, scanner changes, paper-entry
permissions, or trade recommendations in this project. Every output remains
`decision: NO_TRADE`, `order_approval: false`, `model_calibrated: false`, and
all target probabilities unavailable. Phase 2's live evaluator and Phase 3's
paper controller remain separate and blocked by the original validation gates.

## Source roles and bounded collection

- **Massive:** roster/identity only. On predeclared dates within the account's
  actual entitlement, query US stock tickers with an explicit date and active
  status. Follow validated, same-host pagination to completion within
  predeclared request/page caps; a partial result never becomes a complete
  universe. Record request date, fetch time, response provenance, exchange,
  security type, ticker, CIK/FIGI when available, active status, and delisting
  metadata. Query `active=false` to assess known failures and compare with
  the dated active universe; it is not a substitute for historical membership
  on dates when those issuers traded. Treat ticker-event history as
  experimental corroboration, not guaranteed complete continuity. Missing
  identifiers, symbol reuse, conflicting company identity or inaccessible
  delisted names block the affected row.
- **IBKR:** retain the existing bounded, read-only historical minute/hour
  trade bars, date-only daily coverage and bid/ask probe. A daily bar is not
  given an invented as-of intraday timestamp. Only complete minute bars and
  timed quotes can support a later replay's decisions/fill model.
- **Alpaca:** optional historical **news only**, via the existing validated
  Market Data API adapter; never substitute its IEX prices/quotes for missing
  IBKR history. Record provider, article ID, issuer tags, headline, UTC
  publication and fetch time, and requested window. Deduplicate identical
  articles; do not count missing articles as negative news coverage or label
  any article a catalyst solely because it is adjacent to a price move.
  IBKR and Marketaux news observations may coexist, with provider-specific
  timestamp/coverage status retained.

A completed dated roster snapshot is at day resolution, not an intraday
listing-change feed. For a historical intraday candidate, use only membership
whose availability by that decision can be established (e.g., a previously
completed dated snapshot with a verified identity carried forward); reject
same-day changes or intervals whose effective time is unknown. No future
listing status, delisting date, renamed ticker, filing, bar, quote or news
publication may justify an earlier decision.

Collection is opt-in and requires an external predeclared sample manifest
before any request. Keep source-specific bounded pages, dates, timeouts and
rate-limit handling; stop with explicit reason on malformed responses,
redirects to an untrusted host, missing pages or provider refusal. Credentials
come from private environment configuration outside the repository, never
from command arguments, reports, traces or fixtures. Any API keys exposed
in the conversation must be rotated; do not encode or print their values.

## Strict micro-cap gate

The user chose **true issuer market cap at most $300,000,000**, inclusive,
at each historical decision. Compute this only when a suitable raw price was
known by the decision and total relevant outstanding shares have a **public
filing/availability timestamp no later than that decision**. Account for
splits, share classes, ticker changes and price/share adjustment basis before
using the product. An ambiguous filing timestamp, incomplete share-class
coverage, stale price or uncertain identity yields `MARKET_CAP_UNVERIFIED`;
that row cannot be labeled micro-cap for training or eligibility. The
Massive ticker-details market-cap value is not an acceptable shortcut.

This extension investigates whether a legally accessible, filing-date-
qualified share series exists. SEC endpoints were inaccessible from the
current environment during source research; vendor alternatives require
their own availability-time and license checks. **Until such a source is
actually verified, the strict-cap gate stays blocked.** This plan cannot
promise a calibrated model, silently substitute a low-price/volume proxy or
weaken the independent-episode thresholds to compensate.

## Report, tests and handoff

Extend the existing coverage matrix with provider-specific roster and news
status, pagination completeness, dated coverage, unresolved-identity counts,
observed article counts, micro-cap gate status and exact blocking reasons.
Distinguish provider entitlement/connection errors from absent observed
articles; distinguish a roster pilot from a verified historical universe.
Keep the current 60/20/20 chronological holdout, purged whole episodes and
at least 50 comparable independent train-plus-validation episodes in the
**later** pooled-model design; no probabilities are computed here.

Offline tests cover dated active/inactive responses, ticker changes and reused
symbols, missing identifiers, pagination limits/loops/truncation and rate
limits, daily versus intraday timestamps, split/basis mismatch, article
publication after decision, wrong company, duplicate news, empty or blocked
news access, unverified shares and filings published later than the replay
time. No test makes real network or order calls. A bounded real-data report
records observed coverage without claiming completeness from one sample.
If a filing-date share source or a defensible historical roster cannot be
verified, output `NO_TRADE` and document the remaining data requirement;
only then decide whether a separately designed prospective collector or
licensed source can close the gap.
