import os
import re
import threading
import time

from datetime import (
    datetime,
    timedelta,
    timezone,
)

from dotenv import load_dotenv

from ibapi.client import EClient
from ibapi.wrapper import EWrapper
from ibapi.contract import Contract

from ai_market_intelligence import (
    NewsItem,
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

IB_HOST = os.getenv(
    "IB_HOST",
    "127.0.0.1",
)

IB_PORT = int(
    os.getenv(
        "IB_PORT",
        "7496",
    )
)

IB_CLIENT_ID = int(
    os.getenv(
        "IB_NEWS_CLIENT_ID",
        "80",
    )
)

CONNECT_TIMEOUT = 7
CONTRACT_TIMEOUT = 7
NEWS_TIMEOUT = 10

NEWS_LOOKBACK_HOURS = int(
    os.getenv(
        "NEWS_LOOKBACK_HOURS",
        "24",
    )
)

NEWS_RAW_MAX_RESULTS = int(
    os.getenv(
        "NEWS_RAW_MAX_RESULTS",
        "50",
    )
)

NEWS_MAX_RESULTS = int(
    os.getenv(
        "NEWS_MAX_RESULTS",
        "10",
    )
)


# ============================================================
# PROVIDERS
# ============================================================

NEWS_PROVIDERS = [
    "BRFG",
    "BRFUPDN",
    "DJ-N",
    "DJ-RT",
    "DJ-RTA",
    "DJ-RTE",
    "DJ-RTG",
    "DJNL",
]

PROVIDER_CODES = "+".join(
    NEWS_PROVIDERS
)

PROVIDER_NAMES = {
    "BRFG":
        "Briefing.com",

    "BRFUPDN":
        "Briefing.com Analyst Actions",

    "DJ-N":
        "Dow Jones",

    "DJ-RT":
        "Dow Jones Trader News",

    "DJ-RTA":
        "Dow Jones Asia Pacific",

    "DJ-RTE":
        "Dow Jones Europe",

    "DJ-RTG":
        "Dow Jones Global",

    "DJNL":
        "Dow Jones Newsletters",
}


# ============================================================
# GENERIC COMPANY WORDS
# ============================================================

GENERIC_COMPANY_WORDS = {
    "inc",
    "incorporated",
    "corp",
    "corporation",
    "co",
    "company",
    "companies",
    "ltd",
    "limited",
    "plc",
    "holdings",
    "holding",
    "group",
}


# ============================================================
# CONTRACT
# ============================================================

def make_stock_contract(
    symbol,
):
    contract = Contract()

    contract.symbol = (
        symbol.upper()
    )

    contract.secType = "STK"
    contract.exchange = "SMART"
    contract.currency = "USD"

    return contract


# ============================================================
# IBKR APP
# ============================================================

class NewsApp(
    EWrapper,
    EClient,
):

    def __init__(
        self,
    ):
        EClient.__init__(
            self,
            self,
        )

        self.ready = (
            threading.Event()
        )

        self.contract_done = (
            threading.Event()
        )

        self.news_done = (
            threading.Event()
        )

        self.contract_details = []
        self.news = []
        self.errors = []


    def nextValidId(
        self,
        orderId,
    ):
        print(
            "IBKR CONNECTED | "
            f"clientId={IB_CLIENT_ID}",
            flush=True,
        )

        self.ready.set()


    def contractDetails(
        self,
        reqId,
        contractDetails,
    ):
        self.contract_details.append(
            contractDetails
        )


    def contractDetailsEnd(
        self,
        reqId,
    ):
        self.contract_done.set()


    def historicalNews(
        self,
        requestId,
        time_,
        providerCode,
        articleId,
        headline,
    ):
        self.news.append(
            {
                "time":
                    time_,

                "provider_code":
                    providerCode,

                "article_id":
                    articleId,

                "headline":
                    headline,
            }
        )


    def historicalNewsEnd(
        self,
        requestId,
        hasMore,
    ):
        print(
            "NEWS END | "
            f"hasMore={hasMore}",
            flush=True,
        )

        self.news_done.set()


    def error(
        self,
        reqId,
        errorTime,
        errorCode,
        errorString,
        advancedOrderRejectJson="",
    ):
        informational = {
            2104,
            2106,
            2158,
            2108,
            2109,
        }

        if errorCode in informational:
            return

        self.errors.append(
            {
                "req_id":
                    reqId,

                "code":
                    errorCode,

                "message":
                    errorString,
            }
        )

        print(
            "IB ERROR | "
            f"reqId={reqId} | "
            f"code={errorCode} | "
            f"{errorString}",
            flush=True,
        )


# ============================================================
# TIME
# ============================================================

def ibkr_news_time(
    dt,
):
    return (
        dt
        .astimezone(
            timezone.utc
        )
        .strftime(
            "%Y%m%d %H:%M:%S.0"
        )
    )


def parse_news_time(
    value,
):
    if not value:
        return None

    text = str(
        value
    ).strip()

    formats = [
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y%m%d %H:%M:%S.%f",
        "%Y%m%d %H:%M:%S",
    ]

    for fmt in formats:
        try:
            parsed = datetime.strptime(
                text,
                fmt,
            )

            return parsed.replace(
                tzinfo=
                    timezone.utc
            )

        except ValueError:
            continue

    return None


# ============================================================
# TEXT NORMALIZATION
# ============================================================

def clean_headline(
    headline,
):
    text = str(
        headline
        or
        ""
    ).strip()

    text = re.sub(
        r"^\{[^}]+\}",
        "",
        text,
    )

    text = re.sub(
        r"\s+",
        " ",
        text,
    )

    return text.strip()


def normalize_text(
    value,
):
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(
            value
            or
            ""
        ).lower(),
    ).strip()


def useful_company_words(
    long_name,
):
    normalized = (
        normalize_text(
            long_name
        )
    )

    return [
        word
        for word
        in normalized.split()
        if (
            len(
                word
            )
            >=
            4
            and
            word
            not in
            GENERIC_COMPANY_WORDS
        )
    ]


# ============================================================
# PRIMARY SUBJECT HEURISTICS
# ============================================================

COMPARISON_PATTERNS = [
    r"\btops?\b",
    r"\bbeats?\b",
    r"\bversus\b",
    r"\bvs\b",
    r"\bcompared\s+with\b",
    r"\bcompared\s+to\b",
    r"\bovertakes?\b",
    r"\bsurpasses?\b",
    r"\bahead\s+of\b",
    r"\bbehind\b",
]


def ticker_is_explicit(
    headline,
    symbol,
):
    pattern = (
        r"(?<![A-Z0-9])"
        +
        re.escape(
            symbol.upper()
        )
        +
        r"(?![A-Z0-9])"
    )

    return (
        re.search(
            pattern,
            headline.upper(),
        )
        is not None
    )


def starts_with_company(
    headline,
    company_words,
):
    normalized = (
        normalize_text(
            headline
        )
    )

    if not normalized:
        return False

    if not company_words:
        return False

    first_company_word = (
        company_words[
            0
        ]
    )

    return (
        normalized
        ==
        first_company_word
        or
        normalized.startswith(
            first_company_word
            +
            " "
        )
    )


def company_mentioned_early(
    headline,
    company_words,
):
    normalized_words = (
        normalize_text(
            headline
        )
        .split()
    )

    if not normalized_words:
        return False

    early_words = (
        normalized_words[
            :6
        ]
    )

    for word in (
        company_words
    ):
        if word in early_words:
            return True

    return False


def company_is_comparison_target(
    headline,
    company_words,
):
    normalized = (
        normalize_text(
            headline
        )
    )

    if not normalized:
        return False

    for company_word in (
        company_words
    ):
        company_position = (
            normalized.find(
                company_word
            )
        )

        if company_position < 0:
            continue

        prefix = (
            normalized[
                :company_position
            ]
        )

        for pattern in (
            COMPARISON_PATTERNS
        ):
            if re.search(
                pattern,
                prefix,
            ):
                return True

    return False


def primary_subject_score(
    headline,
    symbol,
    company_words,
):
    score = 0

    if ticker_is_explicit(
        headline,
        symbol,
    ):
        score += 4

    if starts_with_company(
        headline,
        company_words,
    ):
        score += 4

    elif company_mentioned_early(
        headline,
        company_words,
    ):
        score += 2

    if company_is_comparison_target(
        headline,
        company_words,
    ):
        score -= 5

    return score


def is_primary_subject(
    headline,
    symbol,
    company_words,
):
    score = (
        primary_subject_score(
            headline,
            symbol,
            company_words,
        )
    )

    return (
        score
        >=
        2
    )


# ============================================================
# CONTRACT RESOLUTION
# ============================================================

def resolve_contract(
    app,
    symbol,
):
    app.contract_details = []

    app.contract_done.clear()

    req_id = 8100

    app.reqContractDetails(
        req_id,
        make_stock_contract(
            symbol
        ),
    )

    if not app.contract_done.wait(
        CONTRACT_TIMEOUT
    ):
        raise RuntimeError(
            f"Contract lookup timeout for "
            f"{symbol}"
        )

    matches = [
        item
        for item
        in app.contract_details
        if (
            item.contract.symbol.upper()
            ==
            symbol.upper()
            and
            item.contract.secType
            ==
            "STK"
            and
            item.contract.currency
            ==
            "USD"
        )
    ]

    if not matches:
        raise RuntimeError(
            f"No US stock contract found for "
            f"{symbol}"
        )

    details = (
        matches[
            0
        ]
    )

    contract = (
        details.contract
    )

    long_name = (
        getattr(
            details,
            "longName",
            "",
        )
        or
        ""
    )

    print(
        "CONTRACT | "
        f"{symbol} | "
        f"conId={contract.conId} | "
        f"exchange={contract.exchange} | "
        f"primaryExchange="
        f"{contract.primaryExchange} | "
        f"longName={long_name}",
        flush=True,
    )

    return {
        "con_id":
            contract.conId,

        "symbol":
            contract.symbol.upper(),

        "long_name":
            long_name,
    }


# ============================================================
# REQUEST NEWS
# ============================================================

def request_news(
    app,
    symbol,
    con_id,
):
    app.news = []

    app.news_done.clear()

    now = datetime.now(
        timezone.utc
    )

    start = (
        now
        -
        timedelta(
            hours=
                NEWS_LOOKBACK_HOURS
        )
    )

    start_time = (
        ibkr_news_time(
            start
        )
    )

    end_time = (
        ibkr_news_time(
            now
        )
    )

    print(
        "NEWS REQUEST | "
        f"{symbol} | "
        f"conId={con_id} | "
        f"providers={PROVIDER_CODES} | "
        f"start={start_time} | "
        f"end={end_time}",
        flush=True,
    )

    req_id = 8200

    app.reqHistoricalNews(
        req_id,
        con_id,
        PROVIDER_CODES,
        start_time,
        end_time,
        NEWS_RAW_MAX_RESULTS,
        [],
    )

    if not app.news_done.wait(
        NEWS_TIMEOUT
    ):
        raise RuntimeError(
            f"News request timeout for "
            f"{symbol}"
        )

    return list(
        app.news
    )


# ============================================================
# FILTER + NORMALIZE
# ============================================================

def normalize_news(
    *,
    symbol,
    long_name,
    rows,
):
    now = datetime.now(
        timezone.utc
    )

    cutoff = (
        now
        -
        timedelta(
            hours=
                NEWS_LOOKBACK_HOURS
        )
    )

    company_words = (
        useful_company_words(
            long_name
        )
    )

    print(
        "COMPANY_WORDS=",
        company_words,
        flush=True,
    )

    raw_count = len(
        rows
    )

    in_window = []

    for row in rows:
        published = (
            parse_news_time(
                row.get(
                    "time"
                )
            )
        )

        if published is None:
            continue

        if published < cutoff:
            continue

        if (
            published
            >
            (
                now
                +
                timedelta(
                    minutes=5
                )
            )
        ):
            continue

        item = dict(
            row
        )

        item[
            "_published"
        ] = published

        in_window.append(
            item
        )

    relevant = []

    for row in in_window:
        headline = (
            clean_headline(
                row.get(
                    "headline"
                )
            )
        )

        if not headline:
            continue

        score = (
            primary_subject_score(
                headline,
                symbol,
                company_words,
            )
        )

        print(
            "HEADLINE CHECK | "
            f"score={score:+d} | "
            f"{headline}",
            flush=True,
        )

        if not is_primary_subject(
            headline,
            symbol,
            company_words,
        ):
            continue

        item = dict(
            row
        )

        item[
            "_clean_headline"
        ] = headline

        item[
            "_relevance_score"
        ] = score

        relevant.append(
            item
        )

    seen = set()

    deduped = []

    for row in sorted(
        relevant,
        key=lambda item:
            (
                item[
                    "_published"
                ],
                item[
                    "_relevance_score"
                ],
            ),
        reverse=True,
    ):
        headline = (
            row[
                "_clean_headline"
            ]
        )

        key = (
            normalize_text(
                headline
            )
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        deduped.append(
            row
        )

    deduped = (
        deduped[
            :NEWS_MAX_RESULTS
        ]
    )

    print(
        "NEWS FILTER | "
        f"raw={raw_count} | "
        f"in_window={len(in_window)} | "
        f"primary_subject="
        f"{len(relevant)} | "
        f"deduped="
        f"{len(deduped)}",
        flush=True,
    )

    result = []

    for row in (
        deduped
    ):
        provider_code = (
            str(
                row.get(
                    "provider_code",
                    "",
                )
            )
            .strip()
        )

        provider_name = (
            PROVIDER_NAMES.get(
                provider_code,
                provider_code,
            )
        )

        published = (
            row[
                "_published"
            ]
            .isoformat()
        )

        result.append(
            NewsItem(
                headline=
                    row[
                        "_clean_headline"
                    ],

                source=
                    provider_name
                    or
                    None,

                published_at=
                    published,

                summary=
                    None,
            )
        )

    return result


# ============================================================
# PUBLIC
# ============================================================

def collect_news_context(
    symbol,
):
    symbol = (
        symbol
        .strip()
        .upper()
    )

    if not symbol:
        raise ValueError(
            "symbol is required"
        )

    app = (
        NewsApp()
    )

    try:
        app.connect(
            IB_HOST,
            IB_PORT,
            clientId=
                IB_CLIENT_ID,
        )

        thread = threading.Thread(
            target=
                app.run,

            daemon=True,
        )

        thread.start()

        if not app.ready.wait(
            CONNECT_TIMEOUT
        ):
            raise RuntimeError(
                "IBKR connection timeout"
            )

        time.sleep(
            0.50
        )

        contract_info = (
            resolve_contract(
                app,
                symbol,
            )
        )

        rows = (
            request_news(
                app,
                symbol,
                contract_info[
                    "con_id"
                ],
            )
        )

        normalized = (
            normalize_news(
                symbol=
                    symbol,

                long_name=
                    contract_info[
                        "long_name"
                    ],

                rows=
                    rows,
            )
        )

        return normalized

    finally:
        if app.isConnected():
            app.disconnect()

        time.sleep(
            0.25
        )


# ============================================================
# SELF TEST
# ============================================================

def main():
    symbol = (
        os.getenv(
            "NEWS_TEST_SYMBOL",
            "AAPL",
        )
        .strip()
        .upper()
    )

    print(
        "========================================"
    )

    print(
        "TRADINGMAX NEWS CONTEXT"
    )

    print(
        "========================================"
    )

    print(
        "SYMBOL=",
        symbol
    )

    print(
        "LOOKBACK_HOURS=",
        NEWS_LOOKBACK_HOURS
    )

    print(
        "RAW_MAX_RESULTS=",
        NEWS_RAW_MAX_RESULTS
    )

    print(
        "MAX_RESULTS=",
        NEWS_MAX_RESULTS
    )

    print(
        "PROVIDERS=",
        PROVIDER_CODES
    )

    print()

    news = (
        collect_news_context(
            symbol
        )
    )

    print()
    print(
        "===== FILTERED NEWS ====="
    )

    if not news:
        print(
            "NONE"
        )

    else:
        for index, item in enumerate(
            news,
            start=1,
        ):
            print(
                f"{index:02d} | "
                f"{item.published_at} | "
                f"{item.source} | "
                f"{item.headline}"
            )

    print()
    print(
        "NEWS_COUNT=",
        len(
            news
        )
    )

    print()
    print(
        "NEWS_CONTEXT_SELF_TEST=PASS"
    )


if __name__ == "__main__":
    main()
