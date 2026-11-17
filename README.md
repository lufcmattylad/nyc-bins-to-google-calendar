# North Yorkshire bin calendar sync

Keeps a Google Calendar up to date with your [North Yorkshire Council bin collection](https://www.northyorks.gov.uk/bin-calendar/lookup)
dates. You can run it on GitHub Actions (free, no PC needed) or on your own machine.

- One all-day event per bin type per date, colour-coded.
- Only touches events it created (they carry a hidden tag).
- If the council moves a date, the old event is removed and the new one is added.
- If the council site can't be read, nothing in your calendar is changed.

> Tested with the Harrogate area. The other former districts use the same bin-calendar
> pages, so they should work too. If yours doesn't, please open an issue.

## 1. Find your settings

1. Look up your address at https://www.northyorks.gov.uk/bin-calendar/lookup.
2. The results URL looks like `https://www.northyorks.gov.uk/bin-calendar/<AREA>/results/<UPRN>`.
   Note the **AREA** (e.g. `Harrogate`) and the **UPRN** (the long number).

## 2. Create a calendar and a Google service account

1. In Google Calendar, create a new calendar (e.g. "Bins").
   Under its **Settings → Integrate calendar**, copy the **Calendar ID**.
2. In https://console.cloud.google.com/, create a project. Then:
   - **APIs & Services → Library**: enable **Google Calendar API**.
   - **IAM & Admin → Service Accounts**: create a service account (it needs no roles).
     Open it, go to **Keys → Add key → JSON**, and download the key file.
3. Back in the calendar's **Settings → Share with specific people**, add the service
   account's email (`...@....iam.gserviceaccount.com`) with **Make changes to events**.
4. Reminders: events created by a service account use the calendar's own default
   notifications. Set them in the calendar's **Settings → All-day event notifications**,
   e.g. "1 day before at 19:00".

## 3a. Run daily on GitHub Actions

1. Fork or clone this repo into your own GitHub account.
2. In **Settings → Secrets and variables → Actions → New repository secret**, add:

   | Secret | Value |
   |---|---|
   | `BIN_AREA` | e.g. `Harrogate` |
   | `BIN_UPRN` | your UPRN |
   | `GOOGLE_CALENDAR_ID` | the Calendar ID from step 2 |
   | `GOOGLE_SERVICE_ACCOUNT_JSON` | the **entire contents** of the downloaded key file |

3. In the **Actions** tab, enable workflows if asked (forks start with them disabled).
   Then run **Sync bin calendar → Run workflow**, ticking *Dry run* the first time.

Secrets never appear in the repo, and GitHub masks them in logs. Logs from public repos
are public, so the script only logs counts there, not dates.

### Trigger it daily with cron-job.org

The workflow has no `schedule:` trigger, because GitHub disables scheduled workflows in
public repos after 60 days without commits. An external scheduler calls the GitHub API instead:

1. GitHub **Settings → Developer settings → Fine-grained personal access tokens → Generate**.
   Set **Repository access** to this repo only, and **Permissions → Actions** to *Read and write*.
2. At https://cron-job.org, create a job:
   - **URL:** `https://api.github.com/repos/<owner>/<repo>/actions/workflows/sync.yml/dispatches`
   - **Advanced → Request method:** `POST`
   - **Headers:**
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <your token>`
     - `X-GitHub-Api-Version: 2022-11-28`
   - **Request body:** `{"ref":"main"}`
   - **Schedule:** daily, e.g. 06:00 Europe/London. Turn on failure notifications.
3. Use **Test run**. HTTP `204` means it worked, and a run appears in the Actions tab.

Tokens expire, so renew yours before the expiry date. A `401` from cron-job.org means it has expired.
If you'd rather use GitHub's built-in scheduler, add a `schedule:` trigger to `sync.yml`.

## 3b. Or run locally (Windows)

```
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
copy .env.example .env          # then edit .env
```

Save the service-account key as `service_account.json` in the project root.
Both `.env` and `service_account.json` are gitignored. Then:

```
.venv\Scripts\python bin_calendar_sync.py --fetch-only   # check the council data
.venv\Scripts\python bin_calendar_sync.py --dry-run      # show calendar changes
.venv\Scripts\python bin_calendar_sync.py                # sync
powershell -ExecutionPolicy Bypass -File scripts\register_task.ps1   # daily task at 07:00
```

Locally you can use your own Google login instead of a service account. Create an OAuth
**Desktop app** client and save it as `credentials.json`; a browser opens on the first run.
With this option `GOOGLE_CALENDAR_ID` can be `primary`. Set the OAuth consent screen to
*In production*, because in *Testing* the login expires after 7 days.
