"""
JMAP client for Fastmail — fetches unread email.

Note: Fastmail does not yet expose JMAP Calendar. Calendar events are fetched
via CalDAV (see caldav_client.py).
"""

import logging
from datetime import datetime

import httpx

logger = logging.getLogger(__name__)

JMAP_SESSION_URL = "https://api.fastmail.com/jmap/session"
MAIL_CAP = "urn:ietf:params:jmap:mail"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
