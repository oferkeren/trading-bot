"""Observed Alpaca news evidence, never a claim of historical provider completeness."""

from __future__ import annotations

from collections.abc import Mapping
from urllib.error import URLError

from microcap_history import AlpacaHistory, CoverageError, parse_utc


def _utc(value: object) -> str:
    return parse_utc(value).isoformat().replace("+00:00", "Z")


def observed_news(
    client: AlpacaHistory, symbol: str, start: str, end: str, *, max_pages: int | None = None
) -> dict[str, object]:
    """Retain article provenance without inferring a catalyst or complete news coverage."""
    start_at, end_at = parse_utc(start), parse_utc(end)
    if start_at >= end_at:
        raise CoverageError("NEWS_WINDOW_INVALID: start must precede end")
    request_start, request_end = _utc(start), _utc(end)
    try:
        if max_pages is None:
            records = client.fetch_news([symbol], start, end)
        else:
            records = client.fetch_news([symbol], start, end, max_pages=max_pages)
    except CoverageError as error:
        raise CoverageError(f"NEWS_COVERAGE_UNAVAILABLE: {error}") from error
    except (TimeoutError, ConnectionError, URLError) as error:
        # A bare socket timeout can bypass AlpacaHistory's URLError handling.
        raise CoverageError(
            f"NEWS_COVERAGE_UNAVAILABLE: news fetch failed ({type(error).__name__})"
        ) from None

    articles: list[dict[str, object]] = []
    seen: dict[str | int, dict[str, object]] = {}
    for record in records:
        if not isinstance(record, Mapping):
            raise CoverageError("NEWS_INVALID: article must be a mapping")
        article_id = record.get("id")
        if (isinstance(article_id, bool) or not isinstance(article_id, (str, int))
                or article_id == ""):
            raise CoverageError("NEWS_INVALID: article id must be a non-empty string or integer")
        if not record.get("created_at"):
            raise CoverageError("NEWS_PUBLICATION_UNKNOWN: article has no publication time")
        published_at = parse_utc(record["created_at"])
        fetched_at = parse_utc(record.get("fetched_at"))
        if not start_at <= published_at < end_at:
            raise CoverageError("NEWS_OUT_OF_RANGE: article is outside the requested window")
        if published_at > fetched_at:
            raise CoverageError("NEWS_AFTER_FETCH: article publication follows retrieval")
        tags = record.get("symbols")
        if (not isinstance(tags, list) or any(not isinstance(tag, str) for tag in tags)
                or symbol not in tags):
            raise CoverageError("NEWS_SYMBOL_MISMATCH: article is not tagged with requested symbol")
        if (_utc(record.get("request_start")) != request_start
                or _utc(record.get("request_end")) != request_end):
            raise CoverageError("NEWS_REQUEST_MISMATCH: article belongs to another window")
        for field in ("headline", "source", "summary"):
            if not isinstance(record.get(field), str):
                raise CoverageError(f"NEWS_INVALID: {field} must be a string")
        evidence = {
            "id": article_id,
            "created_at": _utc(record["created_at"]),
            "headline": record["headline"],
            "source": record["source"],
            "symbols": list(tags),
            "summary": record["summary"],
            "fetched_at": _utc(record["fetched_at"]),
            "request_start": request_start,
            "request_end": request_end,
            "data_source": "alpaca_news",
        }
        if article_id in seen:
            if seen[article_id] != evidence:
                raise CoverageError("NEWS_DUPLICATE_CONFLICT: same ID has conflicting evidence")
            continue
        seen[article_id] = evidence
        articles.append(evidence)
    return {
        "symbol": symbol,
        "request_start": request_start,
        "request_end": request_end,
        "coverage_status": "UNVERIFIED_BY_PROVIDER",
        "completeness": "UNVERIFIED_BY_PROVIDER",
        "coverage_reason": "NEWS_COVERAGE_UNVERIFIED",
        "article_count": len(articles),
        "articles": articles,
    }
