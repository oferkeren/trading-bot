"""Bounded, read-only access to SEC submissions and XBRL company facts."""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

from microcap_history import CoverageError


_BASE = "https://data.sec.gov"
_TIMEOUT_SECONDS = 10.0
_MAX_SUBMISSIONS_BYTES = 2 * 1024 * 1024
_MAX_COMPANYFACTS_BYTES = 16 * 1024 * 1024
_READ_CHUNK_BYTES = 64 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def urlopen(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("nonstandard JSON constant")


class SecReader:
    """Fetch one complete, validated pair of SEC documents for a CIK.

    Each response has a finite byte cap. The socket timeout applies per blocking
    operation; the elapsed-time check between chunks is best-effort, not a hard
    total deadline when a single open/read blocks or a peer trickles bytes.
    """

    def __init__(self, user_agent: str):
        if not isinstance(user_agent, str) or not user_agent.strip():
            raise CoverageError("SEC_USER_AGENT_MISSING")
        self.__user_agent = user_agent.strip()

    @classmethod
    def from_environment(cls) -> SecReader:
        return cls(os.environ.get("SEC_USER_AGENT", ""))

    def __repr__(self) -> str:
        return "SecReader(user_agent=<redacted>)"

    __str__ = __repr__

    def fetch(self, cik: str) -> tuple[dict, dict]:
        if (
            not isinstance(cik, str)
            or not 1 <= len(cik) <= 10
            or not cik.isascii()
            or not cik.isdecimal()
        ):
            raise CoverageError("SEC_RESPONSE_INVALID")
        padded = cik.zfill(10)
        submissions = self._get(
            f"{_BASE}/submissions/CIK{padded}.json", max_bytes=_MAX_SUBMISSIONS_BYTES,
        )
        facts = self._get(
            f"{_BASE}/api/xbrl/companyfacts/CIK{padded}.json",
            max_bytes=_MAX_COMPANYFACTS_BYTES,
        )
        return submissions, facts

    def _get(self, url: str, *, max_bytes: int) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.__user_agent,
                "Accept-Encoding": "identity",
                "Accept": "application/json",
            },
            method="GET",
        )
        deadline = time.monotonic() + _TIMEOUT_SECONDS
        try:
            with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                if response.geturl() != url or getattr(response, "status", 200) != 200:
                    raise CoverageError("SEC_ACCESS_UNAVAILABLE")
                chunks = []
                total = 0
                while total <= max_bytes:
                    if time.monotonic() >= deadline:
                        raise CoverageError("SEC_ACCESS_UNAVAILABLE")
                    try:
                        chunk = response.read(min(_READ_CHUNK_BYTES, max_bytes + 1 - total))
                    except http.client.HTTPException:
                        raise CoverageError("SEC_RESPONSE_INVALID") from None
                    if not isinstance(chunk, bytes):
                        raise CoverageError("SEC_RESPONSE_INVALID")
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes:
                        raise CoverageError("SEC_RESPONSE_TOO_LARGE")
                    chunks.append(chunk)
                body = b"".join(chunks)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise CoverageError("SEC_RATE_LIMITED") from None
            raise CoverageError("SEC_ACCESS_UNAVAILABLE") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CoverageError("SEC_ACCESS_UNAVAILABLE") from None
        except (AttributeError, TypeError, ValueError):
            raise CoverageError("SEC_RESPONSE_INVALID") from None

        if not isinstance(body, bytes):
            raise CoverageError("SEC_RESPONSE_INVALID")
        if len(body) > max_bytes:
            raise CoverageError("SEC_RESPONSE_TOO_LARGE")
        try:
            document = json.loads(body, parse_constant=_reject_non_json_constant)
        except (ValueError, UnicodeDecodeError, RecursionError):
            raise CoverageError("SEC_RESPONSE_INVALID") from None
        if not isinstance(document, dict):
            raise CoverageError("SEC_RESPONSE_INVALID")
        return document
