# PKU Court Booking Agent

Automated booking for the Peking University venue reservation system
(<https://epe.pku.edu.cn/venue/home>). Logs in with a saved Playwright profile,
opens the venue page, fills the form, solves the booking captcha, and submits —
either on demand from the CLI or on a daily schedule from the bundled web UI.

The flow stops before payment: the agent reserves the court; you pay manually
in the WeChat / Alipay popup afterwards.

## Quick start (fresh machine)

```bash
# 1. clone + enter the repo
git clone <this-repo-url> pku-court-booking-agent
cd pku-court-booking-agent

# 2. python env (3.10+ recommended)
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 3. dependencies + Chromium for Playwright
pip install -r requirements.txt
playwright install chromium

# 4. fill in your credentials and booking targets — see "Configuration" below
cp config/cli/user_config.example.yaml      config/cli/user_config.yaml
cp config/webapp/accounts.example.yaml      config/webapp/accounts.yaml
cp config/webapp/test/user_config.example.yaml      config/webapp/test/user_config.yaml
cp config/webapp/scheduled/user_config.example.yaml config/webapp/scheduled/user_config.yaml
$EDITOR config/cli/user_config.yaml config/webapp/accounts.yaml \
        config/webapp/test/user_config.yaml \
        config/webapp/scheduled/user_config.yaml
```

All `*.yaml` files above are gitignored; only the `*.example.yaml` siblings
are committed.

## Commands

### CLI (one-shot booking)

```bash
# Run all configured workers (config/cli/user_config.yaml, defaults headless=false).
python main.py

# Pass a specific config file pair.
python main.py -c config/cli/user_config.yaml --site-config config/cli/site_config.yaml

# Print the most recent paid orders for every configured user, then exit.
python main.py --query-orders        # default 10 most recent per user
python main.py --query-orders 25

# Sanity-check a webapp split-config pair without launching a browser.
python main.py --print-split-config test
python main.py --print-split-config scheduled

# Print the DevTools MCP selector-discovery checklist (used when selectors break).
python main.py --print-alignment
```

### Web UI

```bash
python -m web.backend.app
# then open http://127.0.0.1:8000
```

Three tabs:

- **Users & Orders** — lists users from `config/webapp/accounts.yaml` plus a
  per-user "Refresh orders" button.
- **Run Booking** — one-off headless booking, pre-filled from
  `config/webapp/test/user_config.yaml`. The "Run now" button is greyed out
  while the scheduled task is running or in its prep window.
- **Scheduled Task** — singleton daily task driven by
  `config/webapp/scheduled/`. Edit the YAML and restart the webapp to
  reconfigure. Last run's full log persists across restarts.

The webapp is bound to `127.0.0.1` only and has no auth; do not expose it
publicly without putting auth + HTTPS in front of it.

## Configuration overview

```
config/
  cli/                      ← used by `python main.py`
    user_config.yaml        (gitignored)
    user_config.example.yaml
    site_config.yaml
  webapp/
    accounts.yaml           (gitignored — shared credentials)
    accounts.example.yaml
    test/                   ← Tab 2 "Run Booking"
      user_config.yaml      (gitignored)
      user_config.example.yaml
      site_config.yaml
    scheduled/              ← Tab 3 "Scheduled Task"
      user_config.yaml      (gitignored)
      user_config.example.yaml
      site_config.yaml
```

`site_config.yaml` (per set) holds selectors, base URL, browser settings, and
defaults. `user_config.yaml` (per set) holds workers — which user books which
slot on which date. The webapp uses `accounts.yaml` as the shared source for
credentials so neither test nor scheduled YAMLs need passwords.

For details on selectors, the captcha pipeline, login methods, and the
stage-gate model, see `CLAUDE.md`.
