## PKU Court Booking — Web Control Panel: Design

A small self-hosted FastAPI control panel that wraps the existing CLI agent
(`src/booking/`) so a single user can:

- View the configured users and browse their recent paid orders.
- Trigger a one-off "test booking" run from the browser.
- Watch the always-on daily scheduled booking task and tail its logs.

Single trusted user. Headless-only. No payment handling — the app reserves the
court; the user pays manually afterwards.

---

### 1. Folder layout

```
web/
  DESIGN.md          ← this file
  backend/
    app.py           ← FastAPI entry — `python -m web.backend.app`
    config_loader.py ← resolves the test/scheduled split-config sets
    jobs.py          ← in-process job manager + `get_booking_lock()`
    scheduler.py     ← singleton daily-fire background task
    routes/          ← (folded into app.py for v1)
    templates/       ← Jinja2 HTML, server-rendered
    static/          ← plain CSS + a small vanilla-JS file for polling
```

Backend imports the existing booking modules (`from src.booking import …`).
No business logic moves — `web/backend/` is purely orchestration and HTTP.

---

### 2. Config files on disk

All YAML configs live under `config/`. Credentials are factored out into one
shared file so the test and scheduled pairs only deal with *workers* (which
slots to book for which user), never with passwords:

```
config/
  cli/                          ← used by `python main.py`
    user_config.yaml            (gitignored)
    user_config.example.yaml
    site_config.yaml
  webapp/
    accounts.yaml               (gitignored — shared credentials)
    accounts.example.yaml
    test/                       ← Tab 2 "Run Booking"
      user_config.yaml          (gitignored)
      user_config.example.yaml
      site_config.yaml
    scheduled/                  ← Tab 3 "Scheduled Task"
      user_config.yaml          (gitignored)
      user_config.example.yaml
      site_config.yaml
```

**`config/webapp/accounts.yaml`** — the single source of truth for IAAA /
alumni login info. One entry per `name`:

```yaml
users:
  - name: "alice"
    login_method: "alumni"
    account: "13800000000"
    password: "…"
```

**`config/webapp/{test,scheduled}/user_config.yaml`** — only `workers:` (and
optionally overrides like `headless`). Each worker references a `name` from
`accounts.yaml`; if a worker references an unknown name the webapp errors at
startup.

```yaml
workers:
  - user: "alice"
    date: "3-days-later"
    start_time_list: ['09', '10', '11']
```

- Tab 2 "Run Booking" loads `config/webapp/test/` + `accounts.yaml`.
- Tab 3 "Scheduled Task" loads `config/webapp/scheduled/` + `accounts.yaml` at
  webapp startup; changes require editing the YAML and restarting.
- Tab 1 "Users & Orders" reads users from `config/webapp/accounts.yaml`.
- Browser profile is shared: both pairs use `./.browser_profile/user_<name>/`,
  so a successful IAAA login from either flow is reused by the other.
- The CLI (`python main.py`) reads `config/cli/user_config.yaml` and
  `config/cli/site_config.yaml`, so the existing workflow keeps working
  alongside the webapp.

---

### 3. Pages & interaction

Single page, three tabs across the top. Switching tabs is a normal page
navigation (`/`, `/run`, `/schedule`); each tab is its own server-rendered
template. Live regions (log panels, order tables) refresh via small `fetch()`
polling calls to JSON endpoints.

#### Tab 1 — Users & Orders
- Table of users from `accounts.yaml`: name, login method, "session valid?"
  hint (derived from whether `.browser_profile/user_<name>` exists).
- The combined order cache loads immediately and displays the last update time.
  It refreshes automatically every day at 08:00 or from the **Refresh orders**
  button. Each order is a compact card with expandable secondary details.

#### Tab 2 — Run Booking (test / one-off)
- Form pre-filled from `config/webapp/test/user_config.yaml`:
  - User dropdown (from `accounts.yaml`)
  - Date picker
  - Start-time priority list (ordered)
- **"Run now"** button → POSTs to `/api/bookings/run`, returns a `job_id`.
  Disabled while the scheduled task is running or in its prep window
  (`/api/schedule/status` is polled to keep this in sync). The backend also
  enforces the same rule (see §5).
- Live log panel polls `/api/jobs/{id}` every 1 s.
- Always headless. No headed toggle.

#### Tab 3 — Scheduled Task (singleton, always-on)
- A single, always-running daily task that exists for the lifetime of the
  webapp process. The user never creates or cancels it.
- Status badge: **Waiting** (shows next fire time + countdown) or **Running**.
- Log panel below:
  - While **Running**: live tail of the current run, polled every 1 s.
  - While **Waiting**: shows the *complete log of the last run*, loaded from
    disk so the panel survives webapp restarts.
- Parameters come from `config/webapp/scheduled/site_config.yaml`
  (`scheduled_time`, `scheduled_prep_seconds`) and
  `config/webapp/scheduled/user_config.yaml` (`workers`). To change them, the
  user edits the YAML and restarts the webapp.
- **Fire time vs scheduled time.** `scheduled_time` (e.g. `"120000"`) is the
  wall-clock instant at which the booking submission must happen — the exact
  moment courts open. The scheduler fires earlier so login + navigation +
  captcha solving have time to finish:
  - **fire_time = scheduled_time − scheduled_prep_seconds** (default 90s)
  - The booking flow itself waits until exactly `scheduled_time` before
    clicking the final submit button (existing `scheduled_mode` in
    `src/booking/runner.py`).

---

### 4. Frontend stack

Server-rendered Jinja2 templates from FastAPI + Pico CSS + a small hand-written
`app.js` for log/orders polling. Pico is vendored locally; there is no build
step or runtime CDN dependency.

This is a single-user personal tool; the dynamic parts are narrow (poll an
endpoint, replace innerHTML of one div), so React/Vite would be overkill.

---

### 5. Backend API + concurrency

| Method | Path                     | Returns                                          |
|--------|--------------------------|--------------------------------------------------|
| GET    | `/`                      | HTML — Users & Orders tab                        |
| GET    | `/run`                   | HTML — Run Booking tab                           |
| GET    | `/schedule`              | HTML — Scheduled Task tab                        |
| GET    | `/api/users`             | JSON: users + session-valid hint                 |
| POST   | `/api/orders/refresh_all`| JSON: `{job_id}` (body: `{limit}`)               |
| GET    | `/api/orders/cache`      | JSON: cached orders + update/scheduler metadata  |
| POST   | `/api/bookings/run`      | JSON: `{job_id}` (body: worker spec)             |
| GET    | `/api/jobs/{id}`         | JSON: `{status, logs[], result}`                 |
| GET    | `/api/schedule/status`   | JSON: `{state, next_fire, no_test_window_active, logs[]}` |

**Concurrency.** Test and scheduled runs must never run concurrently — they
fight over the per-user `.browser_profile/user_<name>/` lock and the upstream
session.

- A single backend-wide `asyncio.Lock` (`jobs.get_booking_lock()`) is acquired
  for the full duration of *any* booking flow.
- **Test → blocked by scheduled.** If the lock is held when
  `POST /api/bookings/run` arrives, the request returns HTTP 409. The Tab 2
  button is also greyed in the UI as a first line of defense.
- **Scheduled protected by a no-test window.** A test run is rejected
  whenever "now" is within `scheduled_prep_seconds + 60s` of the next fire.
  Backend rejects with 409; the button is greyed during this window.

Practical impact: the scheduled run takes ~3 minutes/day plus the prep
window, so test runs are unavailable for ~5 min/day total.

**Job manager** (`web/backend/jobs.py`): one `asyncio.Task` per job, with a
per-job ring-buffer of log lines. A custom `logging.Handler` captures
`src.booking` log calls for the duration of the job.

**Scheduler** (`web/backend/scheduler.py`): a single background `asyncio.Task`
launched at app startup. Loop:

```
while True:
    sleep_until(next_fire_time)            # state = waiting
    async with get_booking_lock():
        job = jobs.start("scheduled_run", run_all_workers)
        await job.done()                    # state = running
    write_meta(data/scheduled_last_run.json, …)
```

---

### 6. Persistent storage

Minimal, file-based. Two files under `data/` (gitignored):

- `data/scheduled_last_run.log` — plain-text log lines from the most recent
  scheduled run. Truncated at the start of each new run, then appended to in
  real time by the same logging handler that feeds the in-memory ring buffer.
- `data/scheduled_last_run.json` — sidecar with `{started_at, finished_at,
  status, result_summary}`. Written once the run finishes.

On webapp startup, the scheduler reads these two files (if present) so the
"Waiting" view shows the previous run's log even after a restart.

`data/orders_cache.json` stores the most recently fetched combined paid-order
list, its last successful update, its last attempt, and per-user refresh
errors. A failed user refresh keeps that user's previous cached orders.

This is intentionally a single-run snapshot, not a history database. If
multi-run history becomes useful (e.g. weekly success-rate view), the natural
upgrade path is a small SQLite file in `data/` with one row per run.

---

### 7. Implementation milestones

Each milestone produces something runnable on its own.

#### M1 — Config refactor
Add `accounts.yaml` + the test/scheduled split. Loader resolves
`(workers + accounts + site) → AppConfig` and errors on unknown `name`
references. CLI flag `--print-split-config {test,scheduled}` for sanity-check.

#### M2 — FastAPI scaffold + Tab 1 (Users & Orders)
FastAPI app bound to `127.0.0.1`, Jinja2 templates, base nav. `GET /` renders
the user list; `POST /api/orders/refresh` + `GET /api/orders/{user}` wire to
existing `fetch_user_orders`. Job manager lands here in minimal form.

#### M3 — Tab 2 Run Booking
`templates/run.html` form pre-filled from `config/webapp/test/`. The
booking flow runs as an asyncio job; Tab 2 polls `/api/jobs/{id}` and tails
logs. Log handler captures `src.booking` loggers into a per-job ring buffer.

#### M4 — Tab 3 Scheduled Task
Background asyncio task computes
`fire_time = scheduled_time − scheduled_prep_seconds`, sleeps, fires, repeats.
`data/scheduled_last_run.{log,json}` written during/after each run; loaded at
startup so Waiting view survives restarts.

#### M5 — Concurrency: booking lock + no-test window
`get_booking_lock()` (asyncio.Lock) acquired by both test and scheduled
runs. `POST /api/bookings/run` rejects with HTTP 409 if in the no-test window
or if the lock is held. Tab 2 button greys when
`/api/schedule/status` reports `no_test_window_active`.

#### M6 — Docs + polish
README quickstart, CLAUDE.md Web UI section, committed example configs.

#### M7 — Two deployment modes + login auth (planned)
Goal: support running the same codebase in two modes, and gate the UI behind
a username + password in both. The single-trusted-user assumption still holds
— there is exactly one account; the password just keeps casual visitors and
network scanners out of the booking controls.

**Two modes**, selected by a `WEBAPP_MODE` env var (or `--mode` flag):

- **`local`** (default) — what we have today. Binds to `127.0.0.1:8000`, no
  TLS, intended for running on the user's own laptop. Auth still required so
  behavior matches remote mode end-to-end and the code path is exercised in
  daily use.
- **`remote`** — runs on a server with a public IP. The FastAPI app still
  binds to `127.0.0.1` (or a unix socket); a reverse proxy (Caddy) in front
  terminates TLS via Let's Encrypt and forwards to the app. The bind host /
  port and the optional `forwarded-allow-ips` setting are configurable so
  the app trusts `X-Forwarded-*` headers only from the proxy.

Mode only affects networking + TLS expectations. The three tabs, the
booking flow, and the scheduler are identical in both modes.

**Auth (both modes).**
- One username + one password, read from env (`WEBAPP_USER`,
  `WEBAPP_PASSWORD_HASH`) or a gitignored `config/webapp/auth.yaml`.
  Password stored as a hash (bcrypt / argon2), never plaintext on disk.
- Cookie-session login: `GET /login` serves a small form,
  `POST /login` verifies and sets a signed session cookie
  (`HttpOnly`, `SameSite=Lax`, `Secure` in remote mode).
- A FastAPI dependency at the app level gates every route except `/login`,
  `/healthz`, and `/static/*`. Unauthed requests to HTML routes redirect
  to `/login`; unauthed JSON requests get HTTP 401.
- No user table, no registration, no password reset UI. To rotate the
  password, edit the env var / yaml and restart.

**Browser profile bootstrap (remote mode).** First IAAA/alumni login per
user requires interactive captcha. Bootstrap path: run `python main.py`
once on a workstation, then `rsync` `.browser_profile/user_<name>/` to the
server. Surfacing captchas through the webapp is out of scope for M7.

**Process supervision (remote mode).** A systemd unit restarts the app on
crash. The scheduler is in-process, so on restart in-memory job state is
lost, but `data/scheduled_last_run.{log,json}` still drives the
Waiting-view recovery.

**Secrets.** `accounts.yaml` and `auth.yaml` stay gitignored; deployment
docs say "scp the configs, don't commit them."

This milestone deliberately does *not* introduce a user table, OAuth, or a
DB — those would change the single-trusted-user assumption the rest of the
app is built around.
