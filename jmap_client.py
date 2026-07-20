"""
JMAP client for Fastmail — fetches unread email and today's calendar events.
"""

import re
import logging
from datetime import datetime, timezone, timedelta

import httpx

logger = logging.getLogger(__name__)

JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"
MAIL_CAP = "urn:ietf:params:jmap:mail"
CAL_CAP = "urn:ietf:params:jmap:calendars"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_duration(duration: str) -> timedelta:
    """Parse an ISO 8601 duration string (e.g. PT1H30M, P1D) to timedelta."""
    m = re.fullmatch(
        r"P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?)?",
        duration or "",
    )
    if not m:
        return timedelta()
    return timedelta(
        weeks=int(m.group(1) or 0),
        days=int(m.group(2) or 0),
        hours=int(m.group(3) or 0),
        minutes=int(m.group(4) or 0),
        seconds=float(m.group(5) or 0),
    )


def _format_event_times(
    start_str: str,
    tz_name: str,
    duration_str: str | None,
    user_tz_str: str,
) -> tuple[str, str]:
    """Return (start_time, end_time) as "HH:MM" strings in the user's timezone."""
    try:
        from zoneinfo import ZoneInfo

        event_tz = ZoneInfo(tz_name) if tz_name else ZoneInfo(user_tz_str)
        user_tz = ZoneInfo(user_tz_str)

        dt_start = datetime.fromisoformat(start_str).replace(tzinfo=event_tz)
        start_local = dt_start.astimezone(user_tz)
        start_fmt = start_local.strftime("%H:%M")

        if duration_str:
            dt_end = dt_start + _parse_duration(duration_str)
            end_local = dt_end.astimezone(user_tz)
            end_fmt = end_local.strftime("%H:%M")
        else:
            end_fmt = ""

        return start_fmt, end_fmt
    except Exception as exc:
        logger.warning("Could not format event time %r: %s", start_str, exc)
        # Fall back: return the raw start string without end
        return start_str[:5] if len(start_str) >= 5 else start_str, ""


def _jmap_responses_by_tag(method_responses: list) -> dict:
    """Index JMAP methodResponses by call tag, checking for errors."""
    by_tag: dict = {}
    for name, result, tag in method_responses:
        if name == "error":
            logger.warning("JMAP error (tag=%s): %s", tag, result)
        else:
            by_tag[tag] = result
    return by_tag


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class JMAPClient:
    def __init__(self, token: str, user_tz: str = "UTC") -> None:
        self._token = token
        self._user_tz = user_tz
        self._headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        self._session: dict | None = None

    async def _get_session(self) -> dict:
        if self._session is None:
            async with httpx.AsyncClient(timeout=10) as http:
                r = await http.get(JMAP_SESSION_URL, headers=self._headers)
                r.raise_for_status()
                self._session = r.json()
        return self._session

    async def get_unread_emails(self, limit: int = 10) -> dict:
        session = await self._get_session()
        account_id = session["primaryAccounts"][MAIL_CAP]
        api_url = session["apiUrl"]

        payload = {
            "using": ["urn:ietf:params:jmap:core", MAIL_CAP],
            "methodCalls": [
                ["Email/query", {
                    "accountId": account_id,
                    "filter": {"notKeyword": "$seen"},
                    "sort": [{"property": "receivedAt", "isAscending": False}],
                    "limit": limit,
                }, "q0"],
                ["Email/get", {
                    "accountId": account_id,
                    "#ids": {
                        "resultOf": "q0",
                        "name": "Email/query",
                        "path": "/ids",
                    },
                    "properties": ["subject", "from", "receivedAt", "preview"],
                }, "g0"],
            ],
        }

        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(api_url, json=payload, headers=self._headers)
            r.raise_for_status()
            data = r.json()

        by_tag = _jmap_responses_by_tag(data["methodResponses"])
        emails = by_tag.get("g0", {}).get("list", [])

        items = []
        for e in emails:
            from_list = e.get("from") or []
            sender = from_list[0] if from_list else {}
            items.append({
                "subject": e.get("subject") or "(no subject)",
                "from": sender.get("name") or sender.get("email", ""),
                "from_email": sender.get("email", ""),
                # RFC3339 timestamp — used directly by Glance's parseRelativeTime
                "received_at": e.get("receivedAt", ""),
                "preview": (e.get("preview") or "").strip(),
            })

        return {"count": len(items), "items": items}

    async def get_today_events(self) -> dict:
        session = await self._get_session()
        caps = session.get("capabilities", {})

        # Resolve calendar capability (standard or fall back to any calendar-like one)
        cal_cap = CAL_CAP if CAL_CAP in caps else next(
            (k for k in caps if "calendar" in k.lower()), None
        )
        if not cal_cap:
            logger.warning("No calendar capability found in JMAP session")
            return {"count": 0, "items": []}

        primary = session.get("primaryAccounts", {})
        account_id = primary.get(cal_cap) or primary.get(MAIL_CAP) or next(
            iter(session["accounts"])
        )
        api_url = session["apiUrl"]

        # Build UTC window for today in the user's timezone
        from zoneinfo import ZoneInfo
        user_tz = ZoneInfo(self._user_tz)
        now_local = datetime.now(user_tz)
        today = now_local.date()
        start_utc = datetime(today.year, today.month, today.day, tzinfo=user_tz).astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=1)

        def _utc_str(dt: datetime) -> str:
            return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        payload = {
            "using": ["urn:ietf:params:jmap:core", cal_cap],
            "methodCalls": [
                ["CalendarEvent/query", {
                    "accountId": account_id,
                    "filter": {
                        "after": _utc_str(start_utc),
                        "before": _utc_str(end_utc),
                    },
                    "limit": 50,
                }, "q1"],
                ["CalendarEvent/get", {
                    "accountId": account_id,
                    "#ids": {
                        "resultOf": "q1",
                        "name": "CalendarEvent/query",
                        "path": "/ids",
                    },
                    "properties": [
                        "title", "start", "timeZone", "duration",
                        "showWithoutTime", "location",
                    ],
                }, "g1"],
            ],
        }

        async with httpx.AsyncClient(timeout=10) as http:
            r = await http.post(api_url, json=payload, headers=self._headers)
            r.raise_for_status()
            data = r.json()

        by_tag = _jmap_responses_by_tag(data["methodResponses"])
        events = by_tag.get("g1", {}).get("list", [])

        items = []
        for e in events:
            all_day = bool(e.get("showWithoutTime", False))
            start_str = e.get("start", "")
            tz_name = e.get("timeZone") or self._user_tz
            duration_str = e.get("duration")

            if all_day or not start_str:
                start_time, end_time = "", ""
            else:
                start_time, end_time = _format_event_times(
                    start_str, tz_name, duration_str, self._user_tz
                )

            items.append({
                "title": e.get("title") or "(no title)",
                "start_time": start_time,
                "end_time": end_time,
                "location": e.get("location") or "",
                "all_day": all_day,
            })

        # Sort by start time (all-day events first, then chronologically)
        items.sort(key=lambda x: (not x["all_day"], x["start_time"]))

        return {"count": len(items), "items": items}
