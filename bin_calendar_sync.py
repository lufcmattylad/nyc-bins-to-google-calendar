"""Sync North Yorkshire Council bin collection dates into Google Calendar.

Fetches the council's bin-calendar Drupal AJAX endpoint, parses the
"Upcoming bin collection days" table, and makes a Google Calendar match it:
  * creates an all-day event per bin type per collection date,
  * deletes previously-synced future events that are no longer listed
    (i.e. the council moved a date),
  * never touches events it did not create, or anything in the past.

All personal settings come from environment variables (or a local .env file);
see .env.example. Nothing property-specific lives in this file.

Usage:
  python bin_calendar_sync.py               # sync
  python bin_calendar_sync.py --fetch-only  # just print the parsed dates
  python bin_calendar_sync.py --dry-run     # show calendar changes, don't make them
"""

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (KEY=value lines). Real env vars take precedence."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(BASE_DIR / ".env")

# --------------------------------------------------------------------------
# Configuration (from environment / .env)
# --------------------------------------------------------------------------
AREA = os.environ.get("BIN_AREA", "").strip()  # e.g. Harrogate
UPRN = os.environ.get("BIN_UPRN", "").strip()  # number at end of your bin-calendar URL
CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary").strip()
# Minutes before midnight at the start of collection day. 300 = 7pm the evening before.
# (Only applies with OAuth login - see README for service-account reminders.)
REMINDER_MINUTES = int(os.environ.get("BIN_REMINDER_MINUTES", "300"))

IN_CI = os.environ.get("GITHUB_ACTIONS") == "true"

# Google Calendar colour IDs: 7=Peacock(blue) 8=Graphite 10=Basil(green)
BIN_STYLES = {
    "household waste": {"emoji": "\U0001F5D1", "colorId": "8"},
    "recycling": {"emoji": "♻", "colorId": "7"},
    "garden waste": {"emoji": "\U0001F33F", "colorId": "10"},
}

SERVICE_ACCOUNT_FILE = BASE_DIR / "service_account.json"  # service-account key
CREDENTIALS_FILE = BASE_DIR / "credentials.json"  # OAuth desktop client (local only)
TOKEN_FILE = BASE_DIR / "token.json"  # created by OAuth login
LOG_FILE = BASE_DIR / "bin_sync.log"

SCOPES = ["https://www.googleapis.com/auth/calendar.events"]
SYNC_TAG = "binCalendarSync"  # private extended property marking our events

# Drupal's compressed "libraries already loaded" list. Not an auth token and not
# property-specific, but the endpoint errors if it is missing. If the site changes
# and requests start failing, re-capture it from browser dev tools.
DRUPAL_LIBRARIES = (
    "eJx1jcGOwyAMRH-IhM_ZI7LBS2kMrmzSNH9f2h4aRbsXa2aeNYOlhQhMLYF6PBiH_5HAAslFUfJJ1xvwDFd4uF9pHTYy"
    "qeQPet4IX9ZcFslMIRr5jxzqBzppBV0cy-jPcg9jRXtAaI3UH833BWF0vNH0J0IlSFHXinYimQWBT-ENMk29dKYJB1lO"
    "2HqJyz5dRudYaqL9sosu9qEVSnO2W6fqX4G7F9rMv-9cJa1MT_guivo"
)

log = logging.getLogger("bin_sync")


def page_url() -> str:
    return f"https://www.northyorks.gov.uk/bin-calendar/{AREA}/results/{UPRN}"


# --------------------------------------------------------------------------
# Fetch + parse
# --------------------------------------------------------------------------
def fetch_results_html(attempts: int = 3, delay_seconds: int = 30) -> str:
    """POST to the Drupal AJAX endpoint and return the inserted results HTML."""
    body = urllib.parse.urlencode(
        {
            "js": "true",
            "_drupal_ajax": "1",
            "ajax_page_state[theme]": "northyorks_base",
            "ajax_page_state[theme_token]": "",
            "ajax_page_state[libraries]": DRUPAL_LIBRARIES,
        }
    ).encode()
    headers = {
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": page_url(),
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) bin-calendar-sync/1.0",
    }
    ajax_url = f"{page_url()}/ajax?_wrapper_format=drupal_ajax"

    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            req = urllib.request.Request(ajax_url, data=body, headers=headers, method="POST")
            with urllib.request.urlopen(req, timeout=30) as resp:
                commands = json.loads(resp.read().decode("utf-8"))
            for cmd in commands:
                if cmd.get("command") == "insert" and "upcoming-collection" in (cmd.get("data") or ""):
                    return cmd["data"]
            raise ValueError("No 'insert' command containing the collection table in response")
        except Exception as exc:  # noqa: BLE001 - retry on anything, report last
            last_error = exc
            log.warning("Fetch attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                time.sleep(delay_seconds)
    raise RuntimeError(f"Could not fetch bin collection data: {last_error}")


def _strip_tags(fragment: str) -> str:
    return html.unescape(re.sub(r"<[^>]+>", " ", fragment)).strip()


def parse_collections(results_html: str) -> list[tuple[dt.date, str]]:
    """Return sorted (date, bin_type) pairs from the upcoming-collection table."""
    section = re.search(r'id="upcoming-collection".*?<tbody>(.*?)</tbody>', results_html, re.S)
    if not section:
        raise ValueError("Upcoming collection table not found in HTML")

    collections = set()
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", section.group(1), re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.S)
        if len(cells) < 3:
            continue
        date = dt.datetime.strptime(_strip_tags(cells[0]), "%d %B %Y").date()
        # A row can list several bins separated by <br>
        for part in re.split(r"<br\s*/?>", cells[2]):
            bin_type = " ".join(_strip_tags(part).split())
            if bin_type:
                collections.add((date, bin_type))

    if not collections:
        raise ValueError("Collection table parsed but contained no rows")
    return sorted(collections)


# --------------------------------------------------------------------------
# Google Calendar
# --------------------------------------------------------------------------
def _service_account_info() -> dict | None:
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    if raw:
        return json.loads(raw)
    if SERVICE_ACCOUNT_FILE.exists():
        return json.loads(SERVICE_ACCOUNT_FILE.read_text(encoding="utf-8"))
    return None


def _oauth_credentials():
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        creds.refresh(Request())
    else:
        if IN_CI:
            raise SystemExit("No Google credentials: set the GOOGLE_SERVICE_ACCOUNT_JSON secret.")
        if not CREDENTIALS_FILE.exists():
            raise SystemExit(
                "No Google credentials found. Add service_account.json (recommended) or an "
                "OAuth desktop client as credentials.json - see README.md."
            )
        flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
        creds = flow.run_local_server(port=0)
    TOKEN_FILE.write_text(creds.to_json(), encoding="utf-8")
    return creds


def get_calendar_service():
    from googleapiclient.discovery import build

    info = _service_account_info()
    if info:
        from google.oauth2 import service_account

        if CALENDAR_ID == "primary":
            raise SystemExit(
                "With a service account, GOOGLE_CALENDAR_ID must be the ID of a calendar "
                "shared with the service account ('primary' is the service account's own, "
                "invisible calendar)."
            )
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = _oauth_credentials()
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def event_key(date: dt.date, bin_type: str) -> str:
    return f"{date.isoformat()}|{bin_type.lower()}"


def build_event(date: dt.date, bin_type: str) -> dict:
    style = BIN_STYLES.get(bin_type.lower(), {"emoji": "\U0001F5D1", "colorId": None})
    event = {
        "summary": f"{style['emoji']} {bin_type}",
        "description": f"Put out by 6am.\n{page_url()}",
        "start": {"date": date.isoformat()},
        "end": {"date": (date + dt.timedelta(days=1)).isoformat()},
        "transparency": "transparent",  # don't show as busy
        "reminders": {
            "useDefault": False,
            "overrides": [{"method": "popup", "minutes": REMINDER_MINUTES}],
        },
        "extendedProperties": {
            "private": {SYNC_TAG: "1", "binKey": event_key(date, bin_type)}
        },
    }
    if style["colorId"]:
        event["colorId"] = style["colorId"]
    return event


def list_synced_events(service, start: dt.date, end: dt.date) -> list[dict]:
    events, page_token = [], None
    while True:
        resp = (
            service.events()
            .list(
                calendarId=CALENDAR_ID,
                privateExtendedProperty=f"{SYNC_TAG}=1",
                timeMin=f"{start.isoformat()}T00:00:00Z",
                timeMax=f"{end.isoformat()}T23:59:59Z",
                singleEvents=True,
                maxResults=250,
                pageToken=page_token,
            )
            .execute()
        )
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            return events


def sync(collections: list[tuple[dt.date, str]], dry_run: bool) -> None:
    today = dt.date.today()
    wanted = {event_key(d, t): (d, t) for d, t in collections if d >= today}
    if not wanted:
        log.info("No upcoming collections to sync")
        return
    # Only reconcile within the window the council currently lists. Anything before
    # its first date is history (e.g. today's row dropped once collected) and
    # anything after its last date hasn't been published yet - leave both alone.
    first_date = min(d for d, _ in wanted.values())
    last_date = max(d for d, _ in wanted.values())

    service = get_calendar_service()
    existing = list_synced_events(service, first_date, last_date)

    existing_by_key = {}
    to_delete = []
    for ev in existing:
        key = ev.get("extendedProperties", {}).get("private", {}).get("binKey")
        ev_date = dt.date.fromisoformat(ev["start"].get("date", "1970-01-01"))
        if ev_date < first_date or ev_date > last_date:
            continue  # outside the window the council gave us - leave alone
        if key in wanted and key not in existing_by_key:
            existing_by_key[key] = ev
        else:
            to_delete.append(ev)  # date moved, or a duplicate

    to_create = [v for k, v in wanted.items() if k not in existing_by_key]

    # Per-event lines are DEBUG so public CI logs only show counts.
    prefix = "[dry-run] " if dry_run else ""
    for d, t in sorted(to_create):
        log.debug("%sCreate: %s %s", prefix, d.isoformat(), t)
        if not dry_run:
            service.events().insert(calendarId=CALENDAR_ID, body=build_event(d, t)).execute()
    for ev in to_delete:
        log.debug("%sDelete: %s", prefix,
                  ev.get("extendedProperties", {}).get("private", {}).get("binKey"))
        if not dry_run:
            service.events().delete(calendarId=CALENDAR_ID, eventId=ev["id"]).execute()

    log.info("%sDone: %d created, %d deleted, %d unchanged",
             prefix, len(to_create), len(to_delete), len(existing_by_key))


# --------------------------------------------------------------------------
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--fetch-only", action="store_true", help="print parsed dates and exit")
    parser.add_argument("--dry-run", action="store_true", help="show calendar changes without making them")
    parser.add_argument("--list", action="store_true", help="list upcoming synced events in the calendar")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emoji on Windows consoles
    handlers = [logging.StreamHandler(sys.stdout)]
    if not IN_CI:
        handlers.append(logging.FileHandler(LOG_FILE, encoding="utf-8"))
    logging.basicConfig(
        level=logging.INFO if IN_CI else logging.DEBUG,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # Keep third-party libraries quiet even at DEBUG.
    for noisy in ("googleapiclient", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    missing = [name for name, val in (("BIN_AREA", AREA), ("BIN_UPRN", UPRN)) if not val]
    if missing:
        log.error("Missing required setting(s): %s (set them in .env or as secrets)", ", ".join(missing))
        return 2

    try:
        if args.list:
            today = dt.date.today()
            events = list_synced_events(get_calendar_service(), today, today + dt.timedelta(days=366))
            events.sort(key=lambda e: e["start"].get("date", ""))
            for ev in events:
                print(f"{ev['start'].get('date')}  colour={ev.get('colorId', '-'):>2}  {ev.get('summary', '')}")
            print(f"{len(events)} synced event(s)")
            return 0

        collections = parse_collections(fetch_results_html())
        log.info("Fetched %d collections", len(collections))
        if args.fetch_only:
            for d, t in collections:
                print(f"{d.isoformat()}  {d:%a}  {t}")
            return 0
        sync(collections, dry_run=args.dry_run)
        return 0
    except Exception:  # noqa: BLE001 - log everything for the scheduled run
        log.exception("Bin calendar sync failed")
        return 1


if __name__ == "__main__":
    sys.exit(main())
