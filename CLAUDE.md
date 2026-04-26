# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Automated court booking agent for Peking University's venue reservation system (https://epe.pku.edu.cn/venue/home). Uses Playwright to automate browser interaction: login, venue selection, form filling, CAPTCHA solving, and booking submission.

## Setup & Running

```bash
pip install -r requirements.txt
playwright install chromium
cp user_config.example.yaml user_config.yaml  # then fill in credentials/booking params
python main.py
```

Key CLI flags:
```bash
python main.py -c user_config.yaml --site-config site_config.yaml
python main.py --print-alignment  # Print DevTools MCP selector discovery checklist and exit
```

## Configuration System

Two-file config with deep merge (user values win over site defaults):

- **`site_config.yaml`** — Base URL, Playwright CSS/role/text selectors, browser settings, defaults
- **`user_config.yaml`** — Credentials (via `users` list) and booking params (via `workers` list: `user`, `date`, `start_time_list`), CAPTCHA config

`src/booking/config.py` merges them into an `AppConfig` dataclass. Required top-level keys are validated at load time. Selectors live in `SelectorConfig` (~25 fields); adding/changing a selector only requires editing `site_config.yaml`.

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

## Selector Discovery Workflow

When CSS/role selectors break or need updating, use `--print-alignment` to get the DevTools MCP checklist, then:
1. Run with `headless: false` in `site_config.yaml`
2. Use Playwright DevTools / MCP snapshot tools to find stable locators
3. Update selectors in `site_config.yaml` (never hardcode selectors in Python)

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
