import json
import os
import time

from enum import Enum
from typing import List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
)


BASE_DIR = os.path.dirname(
    os.path.abspath(__file__)
)

load_dotenv(
    os.path.join(
        BASE_DIR,
        ".env",
    )
)


# ============================================================
# CONFIG
# ============================================================

GROQ_API_KEY = (
    os.getenv(
        "GROQ_API_KEY",
        "",
    )
    .strip()
)


AI_PROVIDER = (
    os.getenv(
        "AI_PROVIDER",
        "groq",
    )
    .strip()
    .lower()
)


AI_MODEL = (
    os.getenv(
        "AI_MODEL",
        "openai/gpt-oss-120b",
    )
    .strip()
)


AI_ENABLED = (
    os.getenv(
        "AI_ENABLED",
        "true",
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


AI_FAIL_CLOSED = (
    os.getenv(
        "AI_FAIL_CLOSED",
        "true",
    )
    .strip()
    .lower()
    in {
        "1",
        "true",
        "yes",
        "on",
    }
)


AI_TIMEOUT_SECONDS = float(
    os.getenv(
        "AI_TIMEOUT_SECONDS",
        "8",
    )
)


GROQ_BASE_URL = (
    "https://api.groq.com/openai/v1"
)


# ============================================================
# STRICT BASE MODEL
# ============================================================

class StrictModel(
    BaseModel,
):
    model_config = ConfigDict(
        extra="forbid"
    )


# ============================================================
# ENUMS
# ============================================================

class MarketRegime(
    str,
    Enum,
):
    STRONG_BULL = "STRONG_BULL"
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    STRONG_BEAR = "STRONG_BEAR"


class RiskLevel(
    str,
    Enum,
):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"


class Decision(
    str,
    Enum,
):
    PASS = "PASS"
    REDUCE = "REDUCE"
    BLOCK = "BLOCK"


class EventType(
    str,
    Enum,
):
    NONE = "NONE"
    EARNINGS = "EARNINGS"
    GUIDANCE = "GUIDANCE"
    OFFERING = "OFFERING"
    DILUTION = "DILUTION"
    FDA = "FDA"
    LAWSUIT = "LAWSUIT"
    ACQUISITION = "ACQUISITION"
    CONTRACT = "CONTRACT"
    ANALYST_CHANGE = "ANALYST_CHANGE"
    BANKRUPTCY = "BANKRUPTCY"
    DELISTING = "DELISTING"
    REVERSE_SPLIT = "REVERSE_SPLIT"
    SEC_FILING = "SEC_FILING"
    MANAGEMENT_CHANGE = "MANAGEMENT_CHANGE"
    TRADING_HALT = "TRADING_HALT"
    OTHER = "OTHER"


# ============================================================
# INPUT MODELS
# ============================================================

class MarketContext(
    StrictModel,
):
    spy_change_pct: Optional[float] = None
    qqq_change_pct: Optional[float] = None
    iwm_change_pct: Optional[float] = None
    vix_value: Optional[float] = None
    vix_change_pct: Optional[float] = None
    breadth_pct: Optional[float] = None


class NewsItem(
    StrictModel,
):
    headline: str

    source: Optional[str] = None

    published_at: Optional[str] = None

    summary: Optional[str] = None


class CandidateContext(
    StrictModel,
):
    symbol: str

    action: str

    entry: float

    stop: float

    target: float

    scanner_score: Optional[float] = None

    effective_score: Optional[float] = None

    selection_bucket: Optional[str] = None

    spread_pct: Optional[float] = None

    rsi: Optional[float] = None

    atr: Optional[float] = None

    volr20: Optional[float] = None

    vwap: Optional[float] = None

    market: MarketContext

    news: List[
        NewsItem
    ] = Field(
        default_factory=list
    )


# ============================================================
# OUTPUT MODEL
# ============================================================

class AITradingDecision(
    StrictModel,
):
    decision: Decision

    market_regime: MarketRegime

    market_score: float = Field(
        ge=-1.0,
        le=1.0,
    )

    news_score: float = Field(
        ge=-1.0,
        le=1.0,
    )

    risk_level: RiskLevel

    event_type: EventType

    event_risk: bool

    position_multiplier: float = Field(
        ge=0.0,
        le=1.0,
    )

    confidence: float = Field(
        ge=0.0,
        le=1.0,
    )

    reason_codes: List[str]

    summary: str


# ============================================================
# CLIENT
# ============================================================

_client = None


def get_client():
    global _client


    if AI_PROVIDER != "groq":
        raise RuntimeError(
            f"Unsupported AI provider: "
            f"{AI_PROVIDER}"
        )


    if not GROQ_API_KEY:
        raise RuntimeError(
            "GROQ_API_KEY is missing"
        )


    if _client is None:
        _client = OpenAI(
            api_key=
                GROQ_API_KEY,

            base_url=
                GROQ_BASE_URL,

            timeout=
                AI_TIMEOUT_SECONDS,
        )


    return _client


# ============================================================
# FAIL-CLOSED
# ============================================================

def fail_closed_decision(
    reason,
):
    return AITradingDecision(
        decision=
            Decision.BLOCK,

        market_regime=
            MarketRegime.NEUTRAL,

        market_score=
            0.0,

        news_score=
            0.0,

        risk_level=
            RiskLevel.HIGH,

        event_type=
            EventType.OTHER,

        event_risk=
            True,

        position_multiplier=
            0.0,

        confidence=
            0.0,

        reason_codes=[
            "AI_UNAVAILABLE",
            reason,
        ],

        summary=(
            "AI evaluation unavailable; "
            "trade blocked fail-closed."
        ),
    )


# ============================================================
# DETERMINISTIC SAFETY POLICY
# ============================================================

HARD_BLOCK_EVENTS = {
    EventType.BANKRUPTCY,
    EventType.DELISTING,
    EventType.TRADING_HALT,
}


def apply_deterministic_policy(
    result,
):
    if (
        result.event_type
        in HARD_BLOCK_EVENTS
    ):
        result.decision = (
            Decision.BLOCK
        )

        result.position_multiplier = (
            0.0
        )

        result.event_risk = (
            True
        )


    if (
        result.risk_level
        ==
        RiskLevel.EXTREME
    ):
        result.decision = (
            Decision.BLOCK
        )

        result.position_multiplier = (
            0.0
        )


    if (
        result.decision
        ==
        Decision.BLOCK
    ):
        result.position_multiplier = (
            0.0
        )


    elif (
        result.decision
        ==
        Decision.PASS
    ):
        result.position_multiplier = (
            1.0
        )


    elif (
        result.decision
        ==
        Decision.REDUCE
    ):
        allowed = [
            0.25,
            0.50,
            0.75,
        ]

        result.position_multiplier = min(
            allowed,

            key=lambda value:
                abs(
                    value
                    -
                    result.position_multiplier
                ),
        )


    return result


# ============================================================
# PROMPT
# ============================================================

SYSTEM_PROMPT = """
You are the market-risk intelligence layer for an automated
trading system.

You do NOT generate trades.

You do NOT increase position size.

You do NOT override deterministic safety rules.

You only evaluate an already-generated trading candidate.

Your output decision must be exactly one of:

PASS
REDUCE
BLOCK

PASS means the candidate may continue unchanged.

REDUCE means the candidate may continue with reduced size.

BLOCK means the candidate should not continue.

Evaluate:

1. Broad US market conditions.
2. Small-cap market conditions.
3. Volatility.
4. Company-specific news.
5. Event risk.
6. Whether the proposed trade is unusually risky given the
   supplied context.

For LONG candidates:

A weak broad market should make you more conservative.

For SHORT candidates:

A strongly bullish broad market should make you more
conservative.

A strong overall market does not make bad company-specific
news safe.

For REDUCE:

position_multiplier should be one of:

0.25
0.50
0.75

For PASS:

position_multiplier = 1.0

For BLOCK:

position_multiplier = 0.0

Bankruptcy, delisting and active trading halts represent
extreme event risk.

Do not invent news.

Do not infer company events that are not present in the
supplied news.

If no news is supplied, reflect the uncertainty.

Do not claim that there is no material news unless that fact
was supplied.

Be especially conservative with volatile low-priced and
small-cap securities.

reason_codes should contain short uppercase machine-readable
codes.

summary should be short and factual.
"""


# ============================================================
# SCHEMA
# ============================================================

def build_response_schema():
    schema = (
        AITradingDecision
        .model_json_schema()
    )


    #
    # Groq strict Structured Outputs requires
    # additionalProperties=false on every object.
    #
    # Pydantic extra="forbid" should already emit it,
    # but this recursive normalization guarantees it.
    #

    def normalize(
        node,
    ):
        if isinstance(
            node,
            dict,
        ):
            if (
                node.get(
                    "type"
                )
                ==
                "object"
            ):
                node[
                    "additionalProperties"
                ] = False


            for value in (
                node.values()
            ):
                normalize(
                    value
                )


        elif isinstance(
            node,
            list,
        ):
            for value in node:
                normalize(
                    value
                )


    normalize(
        schema
    )


    return schema


# ============================================================
# EVALUATION
# ============================================================

def evaluate_candidate(
    context,
):
    if not isinstance(
        context,
        CandidateContext,
    ):
        context = (
            CandidateContext
            .model_validate(
                context
            )
        )


    if not AI_ENABLED:
        return fail_closed_decision(
            "AI_DISABLED"
        )


    try:
        client = (
            get_client()
        )


        payload = (
            context
            .model_dump(
                mode="json"
            )
        )


        schema = (
            build_response_schema()
        )


        started = (
            time.monotonic()
        )


        response = (
            client.chat.completions.create(
                model=
                    AI_MODEL,

                messages=[
                    {
                        "role":
                            "system",

                        "content":
                            SYSTEM_PROMPT,
                    },

                    {
                        "role":
                            "user",

                        "content":
                            json.dumps(
                                payload,
                                ensure_ascii=False,
                                separators=(
                                    ",",
                                    ":",
                                ),
                            ),
                    },
                ],

                response_format={
                    "type":
                        "json_schema",

                    "json_schema": {
                        "name":
                            "trading_market_risk",

                        "strict":
                            True,

                        "schema":
                            schema,
                    },
                },
            )
        )


        elapsed = (
            time.monotonic()
            -
            started
        )


        content = (
            response
            .choices[
                0
            ]
            .message
            .content
        )


        if not content:
            raise RuntimeError(
                "LLM returned empty content"
            )


        parsed = (
            AITradingDecision
            .model_validate_json(
                content
            )
        )


        parsed = (
            apply_deterministic_policy(
                parsed
            )
        )


        print(
            "AI EVALUATION | "
            f"{context.symbol} | "
            f"provider={AI_PROVIDER} | "
            f"model={AI_MODEL} | "
            f"decision="
            f"{parsed.decision.value} | "
            f"regime="
            f"{parsed.market_regime.value} | "
            f"risk="
            f"{parsed.risk_level.value} | "
            f"multiplier="
            f"{parsed.position_multiplier:.2f} | "
            f"confidence="
            f"{parsed.confidence:.2f} | "
            f"latency="
            f"{elapsed:.2f}s",
            flush=True,
        )


        return parsed


    except Exception as exc:
        print(
            "AI ERROR |",
            type(exc).__name__,
            "|",
            str(exc),
            flush=True,
        )


        if AI_FAIL_CLOSED:
            return (
                fail_closed_decision(
                    type(exc).__name__
                )
            )


        raise


# ============================================================
# SELF TEST
# ============================================================

def main():
    sample = CandidateContext(
        symbol=
            "TMXTEST",

        action=
            "BUY",

        entry=
            10.00,

        stop=
            9.80,

        target=
            10.40,

        scanner_score=
            85.0,

        effective_score=
            88.0,

        selection_bucket=
            "CORE",

        spread_pct=
            0.20,

        rsi=
            61.0,

        atr=
            0.20,

        volr20=
            2.4,

        vwap=
            9.92,

        market=
            MarketContext(
                spy_change_pct=
                    -1.5,

                qqq_change_pct=
                    -2.0,

                iwm_change_pct=
                    -2.3,

                vix_value=
                    26.0,

                vix_change_pct=
                    14.0,

                breadth_pct=
                    28.0,
            ),

        news=[
            NewsItem(
                headline=(
                    "Company announces "
                    "new customer contract"
                ),

                source=
                    "company",

                published_at=
                    "2026-09-29T08:00:00Z",

                summary=(
                    "Positive company-specific "
                    "announcement."
                ),
            )
        ],
    )


    print(
        "========================================"
    )

    print(
        "TRADINGMAX AI MARKET INTELLIGENCE"
    )

    print(
        "========================================"
    )

    print(
        "PROVIDER=",
        AI_PROVIDER
    )

    print(
        "MODEL=",
        AI_MODEL
    )

    print(
        "AI_ENABLED=",
        AI_ENABLED
    )

    print(
        "FAIL_CLOSED=",
        AI_FAIL_CLOSED
    )

    print()


    result = (
        evaluate_candidate(
            sample
        )
    )


    print()
    print(
        "===== AI RESULT ====="
    )


    print(
        result.model_dump_json(
            indent=2
        )
    )


    print()
    print(
        "===== SELF TEST ====="
    )


    if (
        "AI_UNAVAILABLE"
        in
        result.reason_codes
    ):
        print(
            "AI_SELF_TEST=FAIL"
        )

        raise SystemExit(
            1
        )


    print(
        "AI_SELF_TEST=PASS"
    )


if __name__ == "__main__":
    main()
