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
cp config/webapp/auth.example.yaml                  config/webapp/auth.yaml
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

The webapp has two deployment modes — **local** (your laptop) and **remote**
(a server with a public IP behind an HTTPS proxy). Both modes require login;
auth setup is the same for both.

The three tabs are:

- **Users & Orders** — lists users from `config/webapp/accounts.yaml` and
  cached paid orders. The cache refreshes daily at 08:00, survives restarts,
  and can also be refreshed manually.
- **Run Booking** — one-off headless booking, pre-filled from
  `config/webapp/test/user_config.yaml`. The "Run now" button is greyed out
  while the scheduled task is running or in its prep window.
- **Scheduled Task** — singleton daily task driven by
  `config/webapp/scheduled/`. Edit the YAML and restart the webapp to
  reconfigure. Last run's full log persists across restarts.

#### 1. Set up the login account

There is exactly **one** login account; the username/password protect the UI
from casual visitors and network scans (the app still assumes a single
trusted user). Two ways to configure it:

**Option A — `auth.yaml` (recommended for local + simple remote setups).**

```bash
# 1. Copy the example.
cp config/webapp/auth.example.yaml config/webapp/auth.yaml

# 2. Generate a session secret (used to sign the login cookie). Copy the
#    printed line and paste it as the `secret:` value in auth.yaml.
python -m web.backend.auth secret

# 3. Generate a password hash. You will be prompted for the password twice;
#    nothing is echoed. Copy the printed `pbkdf2_sha256$…` line and paste it
#    as the `password_hash:` value in auth.yaml.
python -m web.backend.auth hash

# 4. Edit auth.yaml: set `username:` to whatever login you want, paste the
#    secret and password_hash from steps 2-3, save.
$EDITOR config/webapp/auth.yaml
```

`auth.yaml` is gitignored — never commit it. Keep it `chmod 600` on shared
machines.

**Option B — environment variables (recommended for systemd / containers).**

Set these three env vars in the unit file / shell that launches the webapp.
They override `auth.yaml` if both are present.

```bash
export WEBAPP_USER='admin'
export WEBAPP_PASSWORD_HASH="$(python -m web.backend.auth hash)"   # paste pw twice
export WEBAPP_SECRET="$(python -m web.backend.auth secret)"
```

**Rotating the password.** Re-run `python -m web.backend.auth hash`, replace
`password_hash:` (or `WEBAPP_PASSWORD_HASH`), restart the webapp. Existing
sessions stay valid until they expire (7 days) or until you also rotate
`secret:` — rotating the secret invalidates every existing session
immediately.

**On the client.** No client-side setup. Open the URL, the webapp redirects
you to `/login`, you type the username + password from above, and the
session cookie is stored by the browser for 7 days.

#### 2. Run in local mode

Local mode binds to `127.0.0.1:8000` over plain HTTP. Use this on your own
laptop.

```bash
python -m web.backend.app
# → open http://127.0.0.1:8000, sign in.
```

#### 3. Run in remote mode (server with public IP)

Remote mode is for putting the webapp on a server (VPS, home server, etc.)
that you reach over the internet. The FastAPI app still binds to
`127.0.0.1:8000` — a reverse proxy (Caddy is shown below; nginx works too)
sits in front of it and handles TLS. The session cookie is marked `Secure`,
so plain HTTP will not work; you **must** have HTTPS in front.

Step-by-step on the server:

```bash
# 0. ssh in. Have a domain name pointed at the server's public IP.

# 1. Clone, create venv, install deps, install Chromium — same as the
#    "Quick start" section above.

# 2. scp your *.yaml configs from your laptop to the server (do NOT commit
#    them). At minimum:
#    config/webapp/accounts.yaml
#    config/webapp/test/user_config.yaml
#    config/webapp/scheduled/user_config.yaml
#    config/webapp/auth.yaml         (or set the env vars below)
#    config/webapp/scheduled/site_config.yaml  (if you've customized it)

# 3. Bootstrap the per-user browser profiles. The first IAAA / alumni login
#    needs an interactive captcha which the webapp does not surface yet, so:
#      a) Run `python main.py` once locally on your laptop to create
#         `.browser_profile/user_<name>/` with a valid session.
#      b) rsync that directory up to the server:
rsync -av .browser_profile/ user@server:/path/to/pku-court-booking-agent/.browser_profile/

# 4. Start the webapp in remote mode. It still listens on 127.0.0.1.
WEBAPP_MODE=remote python -m web.backend.app
```

Then put a TLS proxy in front. Minimal `Caddyfile`:

```caddyfile
booking.example.com {
    reverse_proxy 127.0.0.1:8000
}
```

Caddy obtains a Let's Encrypt cert automatically on first start. Reload it
(`sudo systemctl reload caddy`) and visit `https://booking.example.com` —
you should see the login page.

**Run the webapp under systemd** so it survives reboots and restarts on
crash. Example unit at `/etc/systemd/system/pku-booking.service`:

```ini
[Unit]
Description=PKU Court Booking Webapp
After=network.target

[Service]
User=tianze
WorkingDirectory=/home/tianze/pku-court-booking-agent
Environment=WEBAPP_MODE=remote
# Either rely on config/webapp/auth.yaml, or set these and skip auth.yaml:
# Environment=WEBAPP_USER=admin
# Environment=WEBAPP_PASSWORD_HASH=pbkdf2_sha256$200000$...$...
# Environment=WEBAPP_SECRET=...
ExecStart=/home/tianze/pku-court-booking-agent/.venv/bin/python -m web.backend.app
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pku-booking
sudo journalctl -u pku-booking -f      # tail logs
```

**Optional env vars** for remote mode:

| Variable                       | Default       | Notes                                                                                         |
|--------------------------------|---------------|-----------------------------------------------------------------------------------------------|
| `WEBAPP_HOST`                  | `127.0.0.1`   | Bind host. Keep at `127.0.0.1` unless you really know you want the app itself on a public IP. |
| `WEBAPP_PORT`                  | `8000`        | Bind port.                                                                                    |
| `WEBAPP_FORWARDED_ALLOW_IPS`   | `127.0.0.1`   | Which proxy IPs are trusted for `X-Forwarded-*` headers.                                      |

**Firewall.** Block port 8000 from the public internet — only Caddy (which
listens on 80/443) should be reachable. The webapp itself stays on
loopback.

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
    auth.yaml               (gitignored — webapp login)
    auth.example.yaml
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
