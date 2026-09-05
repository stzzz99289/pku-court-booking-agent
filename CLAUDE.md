# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated court booking agent for Peking University's venue reservation system (https://epe.pku.edu.cn/venue/home). Uses Playwright to automate browser interaction: login, venue selection, form filling, CAPTCHA solving, and booking submission.

## Setup & Running

```bash
pip install -r requirements.txt
playwright install chromium
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
- `config/webapp/test/{user_config,site_config}.yaml` + shared
  `config/webapp/accounts.yaml` — used by the webapp's "Run Booking" tab.
- `config/webapp/scheduled/{user_config,site_config}.yaml` + same
  `accounts.yaml` — used by the webapp's daily scheduled task.

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
    scheduler.py            ← singleton daily-fire background task
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
with `python -m web.backend.auth secret`. The three tabs are Users & Orders,
Run Booking (test), and Scheduled Task.

**Concurrency.** Test runs and scheduled runs both acquire a single
`asyncio.Lock` from `jobs.get_booking_lock()`, so they cannot overlap on the
shared per-user `.browser_profile/user_<name>/`. The "Run now" button on Tab 2
is also greyed (and the API rejects with HTTP 409) while
`scheduler.in_no_test_window()` is true — the window opens
`scheduled_prep_seconds + 60s` before fire and closes when the run finishes.

**Last-run persistence.** The scheduler writes `data/scheduled_last_run.log`
(plain text, truncated each run) and `data/scheduled_last_run.json` (sidecar
with timing + result summary), so the Scheduled Task tab still shows the
previous run's log after a webapp restart.

**Order cache.** The webapp refreshes all users' paid orders every day at
08:00 and persists the combined result in `data/orders_cache.json`. The Users
& Orders tab loads this cache immediately, shows its last update time, and can
start the same refresh manually. Order refreshes acquire the shared booking
lock because they reuse the same persistent browser profiles.

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
- `iaaa` — PKU IAAA SSO (selectors not yet configured)

## Coding Regulations

- The code for `main.py` should be easy to read for user to understand what is happening when the program runs.
- Currently, the code is for local testing (run the python program locally to automatically book a court). However, in the future we are planning to build a service on a website for users to control the court booking behavior and checking booked courts info in the website. So keep that in mind when coding.

## Commit Message Style

- Always use a single-line commit message (subject only, no body paragraphs). Keep it under ~70 chars and start with a `[tag]` prefix matching the existing style: `[feature]`, `[fix]`, `[chore]`, etc. The Co-Authored-By trailer may follow as a separate trailer line.
