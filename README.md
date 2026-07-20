# glance-helper-api

A tiny Docker container that bridges Fastmail (via JMAP) to Glance, exposing two clean JSON endpoints for use with Glance's `custom-api` widget.

| Endpoint | What it returns |
|---|---|
| `GET /mail` | Up to N most-recent unread emails |
| `GET /calendar` | All calendar events for today |
| `GET /health` | `{"status":"ok"}` |

---

## Quick start

### 1. Get a Fastmail API token

Go to **Settings → Security → API Tokens → New token**.  
Grant it read access to **Mail** and **Calendar**.

### 2. Configure

```bash
cp .env.example .env
# edit .env and paste your token + set your timezone
```

### 3. Run

```bash
docker compose up -d
```

The API is now available at `http://localhost:8080`.

---

## Glance setup

Copy the two widget snippets from [`glance-widgets.yml`](glance-widgets.yml) into your `glance.yml`.

If Glance and this container are in the **same Docker Compose project**, the service name `glance-helper-api` resolves automatically.  Otherwise replace the hostname with the machine's IP or DNS name.

Example `glance.yml` snippet:

```yaml
pages:
  - name: Home
    columns:
      - size: small
        widgets:
          # paste widget blocks from glance-widgets.yml here
```

---

## Environment variables

| Variable | Default | Description |
|---|---|---|
| `FASTMAIL_API_TOKEN` | *(required)* | Fastmail API token |
| `TIMEZONE` | `UTC` | Your local timezone (e.g. `America/New_York`) |
| `MAX_EMAILS` | `10` | Max unread emails to return |
| `MAIL_CACHE_TTL` | `300` | Mail cache lifetime in seconds |
| `CAL_CACHE_TTL` | `300` | Calendar cache lifetime in seconds |

---

## Sample JSON responses

**`/mail`**
```json
{
  "count": 2,
  "items": [
    {
      "subject": "Your invoice is ready",
      "from": "Billing Team",
      "from_email": "billing@example.com",
      "received_at": "2024-07-20T09:15:00Z",
      "preview": "Please find your invoice for July attached…"
    }
  ]
}
```

**`/calendar`**
```json
{
  "count": 1,
  "items": [
    {
      "title": "Team standup",
      "start_time": "09:00",
      "end_time": "09:30",
      "location": "Zoom",
      "all_day": false
    }
  ]
}
```
