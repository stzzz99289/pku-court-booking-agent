## PKU Court Booking — Web Control Panel: Design Draft (v2)

This document is a proposal. Nothing here is implemented yet — please review and
push back before any code is written.

---

### 1. Goal

Turn the local CLI agent into a small self-hosted web app where a user can:

- View the configured users and browse their recent paid orders.
- Trigger a one-off "test booking" run (current `python main.py` flow) from the browser.
- Watch the always-on daily scheduled booking task and tail its logs.

**Single trusted user (you). Headless-only. No payment handling — the app only
reserves the court; the user pays manually afterwards.**

Deployment now: localhost. Deployment later: a remote server with a public IP
(see §7 for what changes then).

---

### 2. Scope split: what's v1 vs later

**v1 (this design):**
- One page, three tabs: **Users & Orders**, **Run Booking**, **Scheduled Task**.
- Backend is a thin FastAPI app that reuses `src/booking/` modules directly
  (no rewrite of business logic).
- Jobs are run as in-process asyncio tasks; UI polls a `/jobs/{id}` endpoint
  for status + log tail.
- One always-on daily scheduled task (singleton). No multi-schedule UI.
- Config is split into two pairs on disk (see §3a):
  - `user_config.test.yaml` + `site_config.test.yaml` → used by Tab 2 "Run now".
  - `user_config.scheduled.yaml` + `site_config.scheduled.yaml` → used by the
    daily scheduled task.
  No DB yet, no in-browser config editor.
- All bookings are headless. Headed mode is not exposed.

**Later (out of scope for v1):**
- In-browser editing of `user_config.yaml`.
- Persistent run history (SQLite).
- Auth + HTTPS for remote deployment (see §7).
- WebSocket/SSE log streaming (v1 polls).

---

### 3. Folder layout

New top-level folder is `web/` (sibling of `src/`):

```
web/
  DESIGN.md          ← this file
  backend/           ← FastAPI app, depends on ../src/booking/
    app.py
    routes/
      users.py
      bookings.py
      orders.py
      schedule.py    ← singleton daily task (status + log tail)
    jobs.py          ← in-process job manager (asyncio tasks + log buffer)
    scheduler.py     ← background loop that fires the daily task
    templates/       ← Jinja2 HTML (server-rendered, see §5)
    static/          ← CSS + a small vanilla-JS file for polling
```

Backend imports the existing booking modules (`from src.booking import …`).
No business logic moves — `web/backend/` is purely orchestration and HTTP.

---

### 3a. Config files on disk

All YAML configs live under `config/`. Credentials are factored out into
one shared file so the test and scheduled pairs only deal with *workers*
(which slots to book for which user), never with passwords:

```
config/
  cli/                          ← used by `python main.py`
    user_config.yaml            (gitignored)
    user_config.example.yaml
    site_config.yaml
  webapp/
    accounts.yaml               (gitignored — shared credentials)
    accounts.example.yaml
    test/                       ← Tab 2 "Run Booking" loads this pair + accounts
      user_config.yaml          (gitignored)
      user_config.example.yaml
      site_config.yaml
    scheduled/                  ← Tab 3 "Scheduled Task" loads this pair + accounts
      user_config.yaml          (gitignored)
      user_config.example.yaml
      site_config.yaml
```

**`config/webapp/accounts.yaml`** — the single source of truth for IAAA / alumni login
info. One entry per `name`:

```yaml
users:
  - name: "alice"
    login_method: "alumni"
    account: "13800000000"
    password: "…"
```

**`config/webapp/{test,scheduled}/user_config.yaml`** — only `workers:`
(and optionally overrides like `headless`). Each worker references a `name`
from `accounts.yaml`; if a worker references an unknown name the webapp
errors at startup. No `users:` section in these files anymore.

```yaml
workers:
  - user: "alice"
    date: "3-days-later"
    start_time_list: ['09', '10', '11']
```

- Tab 2 "Run Booking" loads `config/webapp/test/` + `accounts.yaml`.
- Tab 3 "Scheduled Task" loads `config/webapp/scheduled/` + `accounts.yaml`
  at webapp startup; changes require editing the YAML and restarting.
- Tab 1 "Users & Orders" reads users from `config/webapp/accounts.yaml`
  directly — no dedup logic needed since there's only one source.
- Browser profile is shared: both pairs use `./.browser_profile/user_<name>/`,
  so a successful IAAA login from either flow is reused by the other.
- The CLI (`python main.py`) reads `config/cli/user_config.yaml` and
  `config/cli/site_config.yaml`, so the existing workflow keeps working
  alongside the webapp.
- Each gitignored YAML has a committed `*.example.yaml` sibling.

---

### 4. Pages & interaction

**Single page, three tabs across the top.** Switching tabs is a normal page
navigation (`/`, `/run`, `/schedule`); each tab is its own server-rendered
template. Live regions (log panels, order tables) refresh via small `fetch()`
polling calls to JSON endpoints.

#### Tab 1 — Users & Orders
- Table of users from `user_config.yaml`: name, login method, "session valid?"
  hint (derived from whether `.browser_profile/user_<name>` exists and was
  modified recently).
- Per-row button **"Refresh orders"** → kicks off `fetch_user_orders` for that
  user, shows a spinner, then renders the resulting orders below as a table
  (same columns as `format_orders_table`).
- Top-of-tab button **"Refresh all"** → runs the `--query-orders` equivalent
  for every user sequentially.

#### Tab 2 — Run Booking (test / one-off)
- Form pre-filled from `user_config.yaml` workers:
  - User dropdown (from `users:`)
  - Date (date picker, defaults to today + 3 days, mirrors `N-days-later`)
  - Start-time priority list (multi-select of `06`–`21`, ordered)
- **"Run now"** button → POSTs to `/api/bookings/run`, returns a `job_id`.
  - The button is **disabled (greyed out)** while the scheduled task is in
    its `running` state, with a tooltip "scheduled task is running". The
    page polls `/api/schedule/status` every few seconds to keep this in sync.
  - The backend strictly enforces mutual exclusion (see §6a "Concurrency
    rules"), so even a stale page or curl request can't trigger a concurrent
    run.
- Live log panel below: polls `/api/jobs/{id}` every 1 s, appends new log
  lines, shows final status (success / failed / which slot was booked).
- Always headless. No headed toggle.

#### Tab 3 — Scheduled Task (singleton, always-on)
- A single, always-running daily task. The user never creates or cancels it
  — it exists for the lifetime of the webapp process.
- One status badge at the top, two possible states:
  - **Waiting** — not close to fire time. Shows the next scheduled fire time
    and a countdown.
  - **Running** — fire time reached, booking flow in progress.
- One log panel below the badge:
  - While **Running**: live tail of the current run, polled every 1 s.
  - While **Waiting**: shows the *complete log of the last run*, loaded
    from disk (see "Last-run log persistence" below) so the panel survives
    webapp restarts.
- The task's parameters come from `site_config.scheduled.yaml`
  (`scheduled_time`, `scheduled_prep_seconds`) and
  `user_config.scheduled.yaml` (`workers`). To change them, the user edits
  the YAML and restarts the webapp. **No in-browser editing in v1.**
- **Fire time vs scheduled time.** `scheduled_time` (e.g. `"120000"`) is
  the wall-clock instant at which the booking submission must happen — the
  exact moment courts open. The scheduled task can't start *at* that time
  because it still needs to log in, navigate to the venue page, fill the
  form, and solve the booking captcha. So the scheduler fires earlier:
  - **fire_time = scheduled_time − scheduled_prep_seconds**
  - `scheduled_prep_seconds` is a new field in `site_config.scheduled.yaml`
    (**default: 90**). The booking flow itself is responsible for
    waiting until exactly `scheduled_time` before clicking the final
    submit button — this is already how the current `scheduled_mode`
    works in `src/booking/runner.py`, so no booking-logic change is needed.
  - The "Waiting" view shows both: "next fire at 11:59:00, submission at
    12:00:00".

---

### 5. Frontend stack

**Decision: no JS build step.** Server-rendered Jinja2 templates from FastAPI
+ a small hand-written `app.js` (vanilla, no framework) for the polling on the
log panel and order tables. Plain CSS.

Rationale:
- This is a single-user personal tool. React/Vite would add a `node_modules/`,
  a build step, and a separate dev server — overkill here.
- The dynamic parts are narrow: poll an endpoint, replace innerHTML of one
  div. Vanilla `fetch()` handles this in ~30 lines.
- Easier to deploy later: one Python process serves both HTML and JSON; no
  static-build step in CI.

If the UI grows beyond this (e.g. complex form state, drag-to-reorder slot
priorities), we can revisit and bolt on htmx or a real framework then.

---

### 6. Backend API (sketch)

| Method | Path                     | Returns                                          |
|--------|--------------------------|--------------------------------------------------|
| GET    | `/`                      | HTML — Users & Orders tab                        |
| GET    | `/run`                   | HTML — Run Booking tab                           |
| GET    | `/schedule`              | HTML — Scheduled Task tab                        |
| GET    | `/api/users`             | JSON: users + session-valid hint                 |
| POST   | `/api/orders/refresh`    | JSON: `{job_id}` (body: `{user, limit}`)         |
| GET    | `/api/orders/{user}`     | JSON: last cached orders                         |
| POST   | `/api/bookings/run`      | JSON: `{job_id}` (body: worker spec)             |
| GET    | `/api/jobs/{id}`         | JSON: `{status, logs[], result}`                 |
| GET    | `/api/schedule/status`   | JSON: `{state: "waiting"\|"running", next_fire, logs[]}` |

### 6a. Concurrency rules

Test runs and scheduled runs must **never** run concurrently — they'd fight
for the same shared `.browser_profile/user_<name>/` lock and the same
upstream session. Enforced as follows:

- A single backend-wide `asyncio.Lock` (`booking_lock`) is acquired for the
  full duration of *any* booking flow (test or scheduled).
- **Test → blocked by scheduled.** If `booking_lock` is held when
  `POST /api/bookings/run` arrives, the request returns HTTP 409
  `{"error": "scheduled task is running"}`. The Tab 2 button is also
  greyed in the UI as a first line of defense.
- **Scheduled protected by a no-test window.** A test run is rejected
  whenever "now" is within `scheduled_prep_seconds + 60s` of the next
  scheduled fire time (so by default, the last ~2.5 min before fire and
  the entire scheduled-run duration). The Tab 2 button is greyed during
  this window with a tooltip showing when test runs become available
  again. The backend rejects with HTTP 409 as well.
- This avoids the need to forcibly cancel a running test mid-flight (which
  would require clean Chromium teardown across an arbitrary point in the
  booking flow). Trade-off: you can't run a test in the ~2.5 min before
  the daily fire — acceptable since the rest of the day is open.

Practical impact for the user: the scheduled run takes ~3 minutes/day plus
the prep window, so test runs are unavailable for ~5 min/day total.

---

**Job manager** (`web/backend/jobs.py`): one `asyncio.Task` per job, with a
per-job ring-buffer of log lines. A custom `logging.Handler` is attached for
the duration of the job so existing `log.info(...)` calls in `src/booking/`
get captured.

**Scheduler** (`web/backend/scheduler.py`): a single background `asyncio.Task`
launched at app startup. Pseudocode:

```
while True:
    sleep_until(next_fire_time)            # state = waiting
    job = jobs.start("scheduled_run", run_all_workers)
    # while running: log handler tees lines to ring buffer AND appends to
    # data/scheduled_last_run.log (truncated at job start)
    await job.done()                        # state = running while job runs
    write_meta(data/scheduled_last_run.json, {finished_at, result, …})
```

The schedule tab reads `state`, `next_fire`, and either the live job's logs
(while running) or the contents of `data/scheduled_last_run.log` plus its
sidecar `.json` (while waiting).

**Last-run log persistence.** Two files under `data/`:

- `data/scheduled_last_run.log` — plain-text log lines from the most recent
  scheduled run. Truncated at the start of each new run, then appended to in
  real time by the same logging handler that feeds the in-memory ring buffer.
- `data/scheduled_last_run.json` — small sidecar with `{started_at,
  finished_at, status, result_summary}`. Written once the run finishes.

On webapp startup, the scheduler reads these two files (if present) so the
"Waiting" view shows the previous run's log even after a restart.

---

### 7. Remote-deployment considerations (for when v1 ships and you move it off localhost)

Not building these yet, but flagging now so v1 design doesn't paint us into a
corner:

- **Auth.** v1 binds to `127.0.0.1`. Once it's on a public IP, we need at
  minimum HTTP basic auth + HTTPS (Caddy / nginx in front). The FastAPI app
  itself stays single-user — no need for a user table.
- **Browser profile + IAAA login.** Saved sessions live in
  `./.browser_profile/`. On the remote box, the first login per user still
  needs to happen interactively (CAPTCHA). Either: (a) bootstrap the profile
  locally and rsync it up, or (b) expose CAPTCHA images through the web UI
  and let you solve them in-browser. Worth a follow-up design doc.
- **Process lifetime.** The scheduler is in-process. If the webapp crashes
  or is restarted, the in-memory "last run logs" are gone. Acceptable for v1;
  for remote deployment we'd want SQLite-backed run history.

---

### 8. Implementation plan — milestones

Each milestone produces something you can run and review on its own. I'll
stop at the end of each one for you to test before moving on.

#### M1 — Config refactor (no webapp yet)
- Add `accounts.yaml` + example, plus `user_config.{test,scheduled}.yaml`
  + `site_config.{test,scheduled}.yaml` example files.
- In `src/booking/config.py`, add a loader that resolves
  `(workers file + accounts file + site file) → AppConfig` and errors on
  unknown `name` references.
- Existing `user_config.yaml` loader stays untouched so the CLI keeps
  working.
- Add `accounts.yaml` to `.gitignore`.
- **How to test:** a small CLI flag `python main.py --config-set scheduled
  --print-config` (or similar) that loads the new pair and prints the
  resolved workers + credentials sanity check. No browser needed.

#### M2 — FastAPI scaffold + Tab 1 (Users & Orders, read-only)
- `web/backend/app.py`: FastAPI app, Jinja2 templates, static dir, bound
  to `127.0.0.1` only.
- `web/backend/templates/`: `base.html` with the three-tab nav, plus
  `users.html`.
- `GET /` renders user list from `accounts.yaml` + session-valid hint.
- `GET /api/users`, `POST /api/orders/refresh`, `GET /api/orders/{user}`
  wired to existing `fetch_user_orders`.
- Job manager (`jobs.py`) lands here in its minimal form (one in-flight
  job per user is enough for orders refresh).
- **How to test:** `python -m web.backend.app`, open `http://127.0.0.1:8000`,
  click "Refresh orders" for a user, see results render after the job
  completes.

#### M3 — Tab 2 Run Booking (one-off test runs, no concurrency rules yet)
- `templates/run.html` with form pre-filled from `user_config.test.yaml`.
- `POST /api/bookings/run` launches the existing booking flow as a job;
  Tab 2 polls `/api/jobs/{id}` every 1 s and tails logs in a `<pre>` panel.
- Job manager grows: log handler that captures `src.booking` loggers into
  a per-job ring buffer.
- **How to test:** submit a booking from the form, watch the log appear
  live, confirm success/failure shows correctly. (Still no scheduled task
  in this milestone; "Run now" is always enabled.)

#### M4 — Tab 3 Scheduled Task (singleton, disk persistence)
- `web/backend/scheduler.py`: background asyncio task started at app
  boot, computes `fire_time = scheduled_time − scheduled_prep_seconds`,
  sleeps, fires, repeats.
- `data/scheduled_last_run.log` + `.json` written during/after each run;
  loaded at startup so the Waiting view survives restarts.
- `templates/schedule.html` shows status badge, countdown, and the log
  panel (live during running, last-run from disk during waiting).
- `GET /api/schedule/status` returns the JSON the page polls.
- **How to test:** set `scheduled_time` to ~2 min in the future, start
  the webapp, watch state flip Waiting → Running, confirm the log
  panel updates live, restart the webapp afterwards and confirm the
  last-run log is still shown.

#### M5 — Concurrency: booking lock + no-test window
- Add `booking_lock` (asyncio.Lock) in jobs.py; both test and scheduled
  runs acquire it for their duration.
- `POST /api/bookings/run` rejects with HTTP 409 if "now" is within the
  no-test window (`prep + 60s` before fire through end of run) OR the
  lock is held.
- Tab 2 button greys when `/api/schedule/status` reports we're in the
  window, with a tooltip showing when test runs become available.
- **How to test:** set `scheduled_time` close enough that you're inside
  the no-test window, confirm the Run now button is disabled and the
  API rejects requests; verify it re-enables after the run finishes.

#### M6 — Docs + polish
- Update `CLAUDE.md` with a "Web UI" section (folder layout, how to run).
- Update `README.md` with a "Web UI quickstart" subsection.
- Commit example config files; double-check `.gitignore`.
- **How to test:** read the docs, run through the quickstart from a
  fresh clone (or a fresh checkout dir).

Please mark up anything you want changed and I'll revise before writing any
code.
