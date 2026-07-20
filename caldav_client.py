"""
CalDAV client for Fastmail calendar — fetches today's events.

Fastmail does not yet support JMAP Calendar (their API docs confirm CalDAV
is the only calendar protocol they expose). Auth uses an app password, not
an API token. Generate one at:
  Settings → Privacy & Security → Manage app passwords
"""

import logging
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from icalendar import Calendar

logger = logging.getLogger(__name__)

CALDAV_BASE = "https://caldav.fastmail.com"
CALDAV_WELL_KNOWN = f"{CALDAV_BASE}/.well-known/caldav"

DAV = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_text(root: ET.Element, xpath: str) -> str | None:
    el = root.find(xpath)
    return el.text.strip() if el is not None and el.text else None


def _full_url(href: str) -> str:
    return href if href.startswith("http") else CALDAV_BASE + href


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

class CalDAVClient:
    def __init__(self, username: str, app_password: str, user_tz: str = "UTC") -> None:
        self._username = username
        self._auth = (username, app_password)
        self._user_tz = user_tz
        self._calendar_home: str | None = None

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    async def _request(
        self, method: str, url: str, body: str | None = None, depth: str | None = None
    ) -> httpx.Response:
        headers: dict[str, str] = {"Content-Type": "application/xml; charset=utf-8"}
        if depth is not None:
            headers["Depth"] = depth
        async with httpx.AsyncClient(
            auth=self._auth, timeout=15, follow_redirects=True
        ) as http:
            r = await http.request(
                method,
                url,
                content=body.encode("utf-8") if body else None,
                headers=headers,
            )
            r.raise_for_status()
            return r

    async def _propfind(self, url: str, xml_body: str, depth: str = "0") -> ET.Element:
        r = await self._request("PROPFIND", url, body=xml_body, depth=depth)
        return ET.fromstring(r.content)

    # ------------------------------------------------------------------
    # Service discovery
    # ------------------------------------------------------------------

    async def _get_calendar_home(self) -> str:
        if self._calendar_home:
            return self._calendar_home

        # Step 1 — resolve the current-user-principal from /.well-known/caldav
        xml1 = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:propfind xmlns:D="{DAV}">'
            "<D:prop><D:current-user-principal/></D:prop>"
            "</D:propfind>"
        )
        principal_url: str | None = None
        try:
            root1 = await self._propfind(CALDAV_WELL_KNOWN, xml1)
            href = _find_text(root1, f".//{{{DAV}}}current-user-principal/{{{DAV}}}href")
            if href:
                principal_url = _full_url(href)
        except Exception as exc:
            logger.debug("Well-known propfind failed (%s); trying fallback URL", exc)

        if principal_url is None:
            # Common Fastmail fallback
            self._calendar_home = f"{CALDAV_BASE}/dav/calendars/user/{self._username}/"
            logger.debug("Using fallback calendar home: %s", self._calendar_home)
            return self._calendar_home

        # Step 2 — get calendar-home-set from the principal resource
        xml2 = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:propfind xmlns:D="{DAV}" xmlns:C="{CALDAV_NS}">'
            "<D:prop><C:calendar-home-set/></D:prop>"
            "</D:propfind>"
        )
        try:
            root2 = await self._propfind(principal_url, xml2)
            home_href = _find_text(
                root2,
                f".//{{{CALDAV_NS}}}calendar-home-set/{{{DAV}}}href",
            )
            if home_href:
                self._calendar_home = _full_url(home_href)
            else:
                self._calendar_home = f"{CALDAV_BASE}/dav/calendars/user/{self._username}/"
        except Exception as exc:
            logger.warning("Could not fetch calendar-home-set (%s); using fallback", exc)
            self._calendar_home = f"{CALDAV_BASE}/dav/calendars/user/{self._username}/"

        logger.debug("Calendar home: %s", self._calendar_home)
        return self._calendar_home

    async def _list_calendar_urls(self, home_url: str) -> list[str]:
        """Return URLs of individual calendar collections under the home set."""
        xml = (
            '<?xml version="1.0" encoding="utf-8"?>'
            f'<D:propfind xmlns:D="{DAV}" xmlns:C="{CALDAV_NS}">'
            "<D:prop><D:resourcetype/></D:prop>"
            "</D:propfind>"
        )
        try:
            root = await self._propfind(home_url, xml, depth="1")
        except Exception as exc:
            logger.error("Could not list calendars: %s", exc)
            return [home_url]  # fall back to querying the home directly

        calendars: list[str] = []
        for response in root.iter(f"{{{DAV}}}response"):
            href_el = response.find(f"{{{DAV}}}href")
            if href_el is None or not href_el.text:
                continue
            resourcetype = response.find(f".//{{{DAV}}}resourcetype")
            if resourcetype is not None and resourcetype.find(f"{{{CALDAV_NS}}}calendar") is not None:
                url = _full_url(href_el.text.strip())
                if url != home_url.rstrip("/") + "/":  # skip the home itself
                    calendars.append(url)

        return calendars or [home_url]

    # ------------------------------------------------------------------
    # Calendar-query REPORT
    # ------------------------------------------------------------------

    async def _report_events(self, cal_url: str, start_utc: datetime, end_utc: datetime) -> str:
        def _ical_dt(dt: datetime) -> str:
            return dt.strftime("%Y%m%dT%H%M%SZ")

        xml = f"""<?xml version="1.0" encoding="utf-8"?>
<C:calendar-query xmlns:D="{DAV}" xmlns:C="{CALDAV_NS}">
  <D:prop>
    <D:getetag/>
    <C:calendar-data/>
  </D:prop>
  <C:filter>
    <C:comp-filter name="VCALENDAR">
      <C:comp-filter name="VEVENT">
        <C:time-range start="{_ical_dt(start_utc)}" end="{_ical_dt(end_utc)}"/>
      </C:comp-filter>
    </C:comp-filter>
  </C:filter>
</C:calendar-query>"""

        r = await self._request("REPORT", cal_url, body=xml, depth="1")
        return r.text

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def get_today_events(self) -> dict:
        user_tz = ZoneInfo(self._user_tz)
        now_local = datetime.now(user_tz)
        today = now_local.date()
        start_utc = datetime(today.year, today.month, today.day, tzinfo=user_tz).astimezone(timezone.utc)
        end_utc = start_utc + timedelta(days=1)

        try:
            home_url = await self._get_calendar_home()
            cal_urls = await self._list_calendar_urls(home_url)
        except Exception as exc:
            logger.error("CalDAV discovery failed: %s", exc)
            return {"count": 0, "items": []}

        items: list[dict] = []
        for cal_url in cal_urls:
            try:
                xml_text = await self._report_events(cal_url, start_utc, end_utc)
                items.extend(self._parse_report_xml(xml_text, user_tz))
            except Exception as exc:
                logger.warning("REPORT failed for %s: %s", cal_url, exc)

        items.sort(key=lambda x: (not x["all_day"], x["start_time"]))
        return {"count": len(items), "items": items}

    def _parse_report_xml(self, xml_text: str, user_tz: ZoneInfo) -> list[dict]:
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError as exc:
            logger.error("Bad REPORT XML: %s", exc)
            return []

        items: list[dict] = []
        cal_data_tag = f"{{{CALDAV_NS}}}calendar-data"

        for response in root.iter(f"{{{DAV}}}response"):
            cal_el = response.find(f".//{cal_data_tag}")
            if cal_el is None or not cal_el.text:
                continue
            try:
                cal = Calendar.from_ical(cal_el.text)
            except Exception as exc:
                logger.warning("Failed to parse iCal: %s", exc)
                continue

            for component in cal.walk():
                if component.name != "VEVENT":
                    continue
                items.append(self._parse_vevent(component, user_tz))

        return [i for i in items if i is not None]

    def _parse_vevent(self, component, user_tz: ZoneInfo) -> dict | None:
        title = str(component.get("SUMMARY") or "(no title)")
        location = str(component.get("LOCATION") or "")

        dtstart = component.get("DTSTART")
        if dtstart is None:
            return None

        dt = dtstart.dt
        all_day = not isinstance(dt, datetime)

        if all_day:
            return {
                "title": title,
                "start_time": "",
                "end_time": "",
                "location": location,
                "all_day": True,
            }

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=user_tz)
        start_local = dt.astimezone(user_tz)

        end_time_str = ""
        dtend = component.get("DTEND")
        if dtend is not None and isinstance(dtend.dt, datetime):
            dte = dtend.dt
            if dte.tzinfo is None:
                dte = dte.replace(tzinfo=user_tz)
            end_time_str = dte.astimezone(user_tz).strftime("%H:%M")

        return {
            "title": title,
            "start_time": start_local.strftime("%H:%M"),
            "end_time": end_time_str,
            "location": location,
            "all_day": False,
        }
