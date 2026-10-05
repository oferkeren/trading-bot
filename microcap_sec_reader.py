"""Bounded, read-only access to SEC submissions and XBRL company facts."""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request

from microcap_history import CoverageError


_BASE = "https://data.sec.gov"
_TIMEOUT_SECONDS = 10.0
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirectHandler())


def urlopen(request: urllib.request.Request, timeout: float):
    return _OPENER.open(request, timeout=timeout)


def _reject_non_json_constant(value: str) -> None:
    raise ValueError("nonstandard JSON constant")


class SecReader:
    """Fetch one complete, validated pair of SEC documents for a CIK."""

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
        submissions = self._get(f"{_BASE}/submissions/CIK{padded}.json")
        facts = self._get(f"{_BASE}/api/xbrl/companyfacts/CIK{padded}.json")
        return submissions, facts

    def _get(self, url: str) -> dict:
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": self.__user_agent,
                "Accept-Encoding": "identity",
                "Accept": "application/json",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=_TIMEOUT_SECONDS) as response:
                if response.geturl() != url or getattr(response, "status", 200) != 200:
                    raise CoverageError("SEC_ACCESS_UNAVAILABLE")
                body = response.read(_MAX_RESPONSE_BYTES + 1)
        except urllib.error.HTTPError as error:
            if error.code == 429:
                raise CoverageError("SEC_RATE_LIMITED") from None
            raise CoverageError("SEC_ACCESS_UNAVAILABLE") from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise CoverageError("SEC_ACCESS_UNAVAILABLE") from None
        except (AttributeError, TypeError, ValueError):
            raise CoverageError("SEC_RESPONSE_INVALID") from None

        if not isinstance(body, bytes) or len(body) > _MAX_RESPONSE_BYTES:
            raise CoverageError("SEC_RESPONSE_INVALID")
        try:
            document = json.loads(body, parse_constant=_reject_non_json_constant)
        except (ValueError, UnicodeDecodeError):
            raise CoverageError("SEC_RESPONSE_INVALID") from None
        if not isinstance(document, dict):
            raise CoverageError("SEC_RESPONSE_INVALID")
        return document
