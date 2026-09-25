# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated court booking agent for Peking University's venue reservation system (https://epe.pku.edu.cn/venue/home). Uses Playwright to automate browser interaction: login, venue selection, form filling, CAPTCHA solving, and booking submission.

## Setup & Running

```bash
pip install -r requirements.txt
playwright install chromium
# Linux: required for readable Chinese text in order-proof screenshots
sudo apt install fonts-noto-cjk
cp config/cli/user_config.example.yaml config/cli/user_config.yaml  # fill in
python main.py
```

See `README.md` for the full fresh-machine quickstart and the example-file
copy commands for the webapp configs as well.

Key CLI flags:
```bash
python main.py -c config/cli/user_config.yaml --site-config config/cli/site_config.yaml
python main.py --query-orders [N]         # print recent paid orders per user, then exit
python main.py --print-split-config test  # sanity-check a webapp config set
python main.py --print-alignment          # DevTools MCP selector-discovery checklist
```

## Configuration System

Two-file config per set with deep merge (user values win over site defaults).
There are three config sets on disk:

- `config/cli/{user_config,site_config}.yaml` — used by `python main.py`.
- `config/webapp/scheduled/{user_config,site_config}.yaml` + shared
  `config/webapp/accounts.yaml` — the laptop's scheduled-booking source of
  truth and the server's order-query account list.
- `config/webapp/test/` is a legacy one-off set; the dashboard no longer has a
  booking page. CLI one-off tests remain available.

In the webapp pairs, credentials live only in `accounts.yaml`; each `worker`'s
`user:` field references a `name` from accounts, and the loader errors on
unknown names. The CLI pair keeps credentials inline in its own
`user_config.yaml` (one-file UX for local use).

`src/booking/config.py` merges site + user into an `AppConfig` dataclass and
validates required top-level keys at load time. Shared production selectors
live in `src/booking/site_constants.py` and populate `SelectorConfig`; a
`site_config.yaml` may still override them for one-off or environment-specific
testing.

## Architecture

```
main.py
  └── runner.py          # Orchestrates all stages sequentially
        ├── config.py    # Loads + merges YAML configs into AppConfig
        ├── browser.py   # Launches persistent Chromium context (cookies survive runs)
        ├── pipeline.py  # Stage-gate readiness checks; prints hints when selectors are missing
        ├── login.py     # Detects existing session; performs alumni/IAAA login + CAPTCHA
        ├── booking_flow.py  # Filters venue list, fills form, submits, verifies result
        ├── captcha.py   # Pluggable solver: manual (stdin) | env | stub | 2captcha (placeholder)
        └── result.py    # BookingResult dataclass
```

**Stage-gate model:** `pipeline.py` checks whether required selectors/config exist before each stage. If not, it prints human-readable hints and the stage is skipped/aborted. This is the primary mechanism for incremental development — add selectors to `site_config.yaml` to unlock stages.

**Persistent browser profile** (`./.browser_profile`): cookies and session data persist across runs, so re-login is only needed when the session expires.

## Web UI

A small FastAPI control panel lives under `web/` (sibling of `src/`) and
reuses the existing booking modules:

```
web/
  DESIGN.md                 ← design doc (milestones M1–M7)
  backend/
    app.py                  ← FastAPI entry — `python -m web.backend.app`
    auth.py                 ← single-user login (PBKDF2 + signed cookie)
    config_loader.py        ← resolves the test/scheduled split-config sets
    jobs.py                 ← in-process job manager + `get_booking_lock()`
    scheduler.py            ← reusable daily booking worker engine
    local_schedule.py       ← one-shot Windows Task Scheduler entry point
    schedule_sync.py        ← private SSH config/report/heartbeat transport
    templates/, static/     ← Jinja2 + vanilla JS, no build step
```

Two run modes, both require login:

- `python -m web.backend.app` — **local** mode, binds `127.0.0.1:8000`.
- `WEBAPP_MODE=remote python -m web.backend.app` — **remote** mode, still
  binds `127.0.0.1` by default but trusts `X-Forwarded-*` from a reverse
  proxy and marks the session cookie `Secure` (requires HTTPS in front).

Single trusted user. Credentials live in `config/webapp/auth.yaml`
(gitignored) or env (`WEBAPP_USER`, `WEBAPP_PASSWORD_HASH`, `WEBAPP_SECRET`).
Generate a hash with `python -m web.backend.auth hash` and a session secret
with `python -m web.backend.auth secret`. The dashboard has Orders and Schedule
pages. Orders are queried by the server at 13:00 and can be refreshed manually;
there is no dashboard booking endpoint.

`SCHEDULE_EXECUTION_MODE=external` (default) makes the webapp display reports
uploaded by a booking laptop. `SCHEDULE_EXECUTION_MODE=embedded` retains the
original in-process scheduler for a sufficiently powerful Linux host. Run
only one booking host at a time.

**Concurrency.** Order refreshes acquire `jobs.get_booking_lock()` to protect
their browser profiles. Chromium startup is limited to two concurrent launches.
Scheduled workers use durable worker-specific browser profiles, seeded once
from the corresponding per-user profile without disposable Chromium caches,
so duplicate workers for one account never open the same Chromium profile
concurrently and seeding stays out of the critical path.
The Windows one-shot task refuses duplicate or late launches. Its configured workers
start at 11:57 for the 12:00 release. The production server is display-only
for scheduled booking in external mode.

**Last-run persistence and sync.** The booking host writes
`data/scheduled_last_run.{log,json}`, replacing the previous run. Five minutes
after workers finish, the laptop uploads a redacted report via passwordless
SSH. The server atomically stores one `data/schedule_display.json` and the
latest `data/laptop_heartbeat.json`. Check-ins run at 00:00, 04:00, 08:00,
11:45, 16:00, and 20:00 local time; 11:45 syncs the private scheduled configs
before noon preparation. The server marks a heartbeat stale after five hours
and shows a missing-run warning after 12:15 when no completed report arrived.
It cannot directly query a sleeping or NATed laptop.
The Schedule page's on-demand check-in button writes a three-minute request
on the server. A separate lightweight Windows task polls for requests over
outbound SSH once a minute and sends a separate check-in response only when asked.
The response has two independent results: the laptop answered, and a read-only
Windows Task Scheduler check confirms that the daily booking task is enabled
with the expected daily trigger, time, and command. This check does not launch
a booking or guarantee that Windows will be awake at noon. The six routine
check-in times are unchanged; an unanswered request times out.

`scripts/install_windows_schedule.ps1` installs the daily run, routine
heartbeat, and on-demand poll tasks for the signed-in
Windows user. Win+L preserves that session; sleep or sign-out prevents an
interactive task from running. All three tasks use the repo `.venv`'s
windowless `pythonw.exe` and UTF-8 mode; the daily run writes its log to
`data/local_schedule_task.log`. SSH and task-query subprocesses also suppress
console windows. Windowless sync failures rotate through
`data/schedule_sync_task.log`; Task Scheduler retains exit codes. Use
`python.exe` manually when interactive output is needed.
`scripts/deploy_code.ps1` deploys only committed code by fast-forward Git,
restarts the webapp, and syncs private scheduled configs. The SSH destination
can be overridden with `SCHEDULE_SYNC_SSH` and `SCHEDULE_SYNC_REMOTE_DIR`.
Re-run the Windows task installer when scheduled time or prep seconds change.

**Crash diagnostics.** An unexpected runner exception captures a best-effort
full-page screenshot, HTML snapshot, visible-dialog summary, stage, and
traceback under `debugging/crashes/YYYYMMDD/` before its browser context is
closed. Capture operations have short timeouts and run only after a worker has
already crashed; the directory is private and ignored by Git.

Rejected booking CAPTCHAs save the attempted image and coordinate/geometry
metadata, visible widget state, and redacted endpoint metadata under
`debugging/captcha_failures/YYYYMMDD/`. Solver pixels are mapped to the rendered
image box instead of assuming device pixels equal CSS pixels, and only one fresh
challenge is attempted after an invalid-coordinate response to avoid PKU's rate
limit. Keep the Playwright version pinned in `requirements.txt` so Windows and
Linux use the same Chromium generation. Scheduled profiling files retain a
seven-day window. Crash evidence and CAPTCHA diagnostics are never pruned
automatically.

**Order cache.** The server refreshes all users' paid orders every day at
13:00 and persists the combined result in `data/orders_cache.json`. The Orders
page loads this cache immediately, shows its last update time, and can
start the same refresh manually. Order refreshes acquire the shared booking
lock because they reuse the same persistent browser profiles. During a refresh,
current and future normal paid orders receive mobile-card proof screenshots in
`data/order_proofs/`. The `(user, order_no)` index prevents recapture on manual
refreshes; proofs whose use date is before today are removed at the start of
the next refresh. A transient empty table is polled and re-opened before being
accepted; if an established user still returns zero rows, the webapp preserves
that user's previous cache instead of erasing it. Images are exposed only
through the authenticated webapp.

**Session verification.** The Orders page shows the booking host's latest
verification time for each account. In external mode this comes from the
laptop's report; in embedded mode it comes from server browser profiles.
It is historical evidence, not a prediction of future session validity.

## Selector Discovery Workflow

When CSS/role selectors break or need updating, use `--print-alignment` to get the DevTools MCP checklist, then:
1. Run with `headless: false` in the relevant `config/.../site_config.yaml`
2. Use Playwright DevTools / MCP snapshot tools to find stable locators
3. Update shared selectors in `src/booking/site_constants.py`, or use a
   `site_config.yaml` override for a one-off test

## CAPTCHA Solving

Configured via `captcha.provider` in user config. Implementations in `captcha.py`:
- `manual` (default) — prompts stdin
- `env` — reads `CAPTCHA_ANSWER` environment variable
- `stub` — fixed answer for testing
- `twocaptcha` — placeholder, not yet implemented

## Login Methods

- `alumni` — Phone number + password (implemented)
- `student` — PKU student/staff login through IAAA (user ID + password)
- `iaaa` — backward-compatible alias for `student`

Some student/staff accounts do not have a contact number prefilled on the
reservation form. Set `booking_phone` on that user in the private
`accounts.yaml`; alumni users automatically fall back to their phone-number
login when the field is blank.

## Coding Regulations

- The code for `main.py` should be easy to read for user to understand what is happening when the program runs.
- Currently, the code is for local testing (run the python program locally to automatically book a court). However, in the future we are planning to build a service on a website for users to control the court booking behavior and checking booked courts info in the website. So keep that in mind when coding.

## Commit Message Style

- Always use a single-line commit message (subject only, no body paragraphs). Keep it under ~70 chars and start with a `[tag]` prefix matching the existing style: `[feature]`, `[fix]`, `[chore]`, etc. The Co-Authored-By trailer may follow as a separate trailer line.
