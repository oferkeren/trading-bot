import base64
import json
import os
import secrets
import urllib.error
import urllib.request

from pathlib import Path

from dotenv import load_dotenv

from fastapi import (
    Depends,
    FastAPI,
    HTTPException,
)

from fastapi.responses import HTMLResponse

from fastapi.security import (
    HTTPBasic,
    HTTPBasicCredentials,
)

from strategy_status import (
    read_snapshot,
)


BASE_DIR = Path(
    __file__
).resolve().parent


load_dotenv(
    BASE_DIR
    /
    ".env"
)


DASHBOARD_FILE = (
    BASE_DIR
    /
    "dashboard.html"
)


DASHBOARD_USER = os.getenv(
    "DASHBOARD_USER",
    "",
).strip()


DASHBOARD_PASSWORD = os.getenv(
    "DASHBOARD_PASSWORD",
    "",
).strip()


TRADING_API_URL = (
    os.getenv(
        "TRADINGMAX_API_URL",
        "http://127.0.0.1:8000",
    )
    .strip()
    .rstrip("/")
)


app = FastAPI(
    title="TradingMax Dashboard",
    docs_url=None,
    redoc_url=None,
)


security = HTTPBasic()


def dashboard_auth(
    credentials:
        HTTPBasicCredentials
        =
        Depends(
            security
        ),
):

    if (
        not DASHBOARD_USER
        or
        not DASHBOARD_PASSWORD
    ):

        raise HTTPException(
            status_code=503,
            detail=(
                "Dashboard authentication "
                "not configured"
            ),
        )


    username_ok = (
        secrets.compare_digest(
            credentials.username,
            DASHBOARD_USER,
        )
    )


    password_ok = (
        secrets.compare_digest(
            credentials.password,
            DASHBOARD_PASSWORD,
        )
    )


    if (
        not username_ok
        or
        not password_ok
    ):

        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={
                "WWW-Authenticate":
                    "Basic",
            },
        )


    return credentials.username


def backend_auth_header():

    raw = (
        f"{DASHBOARD_USER}:"
        f"{DASHBOARD_PASSWORD}"
    ).encode(
        "utf-8"
    )


    encoded = (
        base64.b64encode(
            raw
        )
        .decode(
            "ascii"
        )
    )


    return (
        f"Basic {encoded}"
    )


def fetch_backend_json(
    path,
):

    url = (
        TRADING_API_URL
        +
        path
    )


    request = (
        urllib.request.Request(
            url,
            method="GET",
            headers={
                "Authorization":
                    backend_auth_header(),

                "Accept":
                    "application/json",

                "User-Agent":
                    "TradingMax-Dashboard/1.0",
            },
        )
    )


    try:

        with urllib.request.urlopen(
            request,
            timeout=5,
        ) as response:

            raw = (
                response.read()
                .decode(
                    "utf-8"
                )
            )


            return json.loads(
                raw
            )


    except urllib.error.HTTPError as exc:

        try:

            body = (
                exc.read()
                .decode(
                    "utf-8",
                    errors="replace",
                )
            )

        except Exception:

            body = ""


        raise HTTPException(
            status_code=502,
            detail=(
                "Trading API returned "
                f"HTTP {exc.code}: "
                f"{body}"
            ),
        )


    except urllib.error.URLError as exc:

        raise HTTPException(
            status_code=503,
            detail=(
                "Trading API unavailable: "
                f"{exc.reason}"
            ),
        )


    except TimeoutError:

        raise HTTPException(
            status_code=504,
            detail=(
                "Trading API timeout"
            ),
        )


    except json.JSONDecodeError as exc:

        raise HTTPException(
            status_code=502,
            detail=(
                "Trading API returned "
                "invalid JSON: "
                f"{exc}"
            ),
        )


@app.get(
    "/health"
)
def health():

    return {
        "status":
            "ok",

        "service":
            "trading-dashboard",

        "backend":
            TRADING_API_URL,
    }


@app.get(
    "/"
)
def root():

    return {
        "service":
            "TradingMax Dashboard",

        "dashboard":
            "/dashboard",

        "health":
            "/health",
    }


@app.get(
    "/dashboard",
    response_class=
        HTMLResponse,
)
def dashboard(
    user=
        Depends(
            dashboard_auth
        ),
):

    if not DASHBOARD_FILE.exists():

        raise HTTPException(
            status_code=500,
            detail=(
                "dashboard.html "
                "not found"
            ),
        )


    return (
        DASHBOARD_FILE
        .read_text(
            encoding="utf-8"
        )
    )


@app.get(
    "/status"
)
def status(
    user=
        Depends(
            dashboard_auth
        ),
):

    return (
        fetch_backend_json(
            "/status"
        )
    )


@app.get(
    "/strategy-status"
)
def strategy_status(
    user=
        Depends(
            dashboard_auth
        ),
):

    return (
        read_snapshot()
    )
