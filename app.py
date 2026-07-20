import logging
import os
import time
from contextlib import asynccontextmanager

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from caldav_client import CalDAVClient
from jmap_client import JMAPClient

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

TOKEN = os.environ.get("FASTMAIL_API_TOKEN", "")
CALDAV_USER = os.environ.get("FASTMAIL_USERNAME", "")
CALDAV_PASS = os.environ.get("FASTMAIL_APP_PASSWORD", "")
TIMEZONE = os.environ.get("TIMEZONE", "UTC")
MAX_EMAILS = int(os.environ.get("MAX_EMAILS", "10"))
MAIL_CACHE_TTL = int(os.environ.get("MAIL_CACHE_TTL", "300"))
CAL_CACHE_TTL = int(os.environ.get("CAL_CACHE_TTL", "300"))

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

_jmap: JMAPClient | None = None
_caldav: CalDAVClient | None = None
_cache: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _jmap, _caldav
    if not TOKEN:
        logger.warning("FASTMAIL_API_TOKEN is not set — /mail will return 503")
    if not CALDAV_USER or not CALDAV_PASS:
        logger.warning(
            "FASTMAIL_USERNAME / FASTMAIL_APP_PASSWORD not set — /calendar will return 503"
        )
    _jmap = JMAPClient(TOKEN, TIMEZONE)
    _caldav = CalDAVClient(CALDAV_USER, CALDAV_PASS, TIMEZONE)
    yield


app = FastAPI(title="Glance Helper API — Fastmail", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Simple TTL cache
# ---------------------------------------------------------------------------

def _get_cached(key: str, ttl: int):
    entry = _cache.get(key)
    if entry and (time.monotonic() - entry["ts"]) < ttl:
        return entry["data"]
    return None


def _set_cached(key: str, data):
    _cache[key] = {"data": data, "ts": time.monotonic()}


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/mail")
async def unread_mail():
    """Return unread emails as JSON suitable for a Glance custom-api widget."""
    if not TOKEN:
        raise HTTPException(503, detail="FASTMAIL_API_TOKEN not configured")

    if (cached := _get_cached("mail", MAIL_CACHE_TTL)) is not None:
        return cached

    try:
        data = await _jmap.get_unread_emails(MAX_EMAILS)
    except Exception as exc:
        logger.error("Failed to fetch mail: %s", exc)
        raise HTTPException(502, detail=f"Upstream JMAP error: {exc}")

    _set_cached("mail", data)
    return data


@app.get("/calendar")
async def today_events():
    """Return today's calendar events as JSON suitable for a Glance custom-api widget."""
    if not CALDAV_USER or not CALDAV_PASS:
        raise HTTPException(
            503,
            detail="FASTMAIL_USERNAME / FASTMAIL_APP_PASSWORD not configured. "
                   "Fastmail calendar requires CalDAV with an app password, not an API token. "
                   "Generate one at: Settings → Privacy & Security → Manage app passwords",
        )

    if (cached := _get_cached("calendar", CAL_CACHE_TTL)) is not None:
        return cached

    try:
        data = await _caldav.get_today_events()
    except Exception as exc:
        logger.error("Failed to fetch calendar: %s", exc)
        raise HTTPException(502, detail=f"Upstream CalDAV error: {exc}")

    _set_cached("calendar", data)
    return data
