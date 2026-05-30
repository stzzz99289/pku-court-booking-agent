# Server Deployment Notes

Webapp + booking agent deployed on the Tencent Cloud VM. These notes cover
how to reach it, how to control the service, and what's quirky about this
particular box.

## Connection

- SSH: `ssh tianze@43.173.124.100`
- Project root on server: `~/pku-court-booking-agent`
- Python venv on server: `~/pku-court-booking-agent/.venv` (Python 3.12)
- Playwright Chromium installed in `~/.cache/ms-playwright/chromium*`

## Dashboard URL

**http://43.173.124.100:18000/**

Login with the credentials defined in `config/webapp/auth.yaml`
(username `tianze` + the password whose PBKDF2 hash is stored there).
The three tabs are Users & Orders, Run Booking (test), and Scheduled Task.

## Service control

A single control script handles start/stop/restart/status/logs:

```bash
# from your local machine
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh start'
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh stop'
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh restart'
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh status'
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh logs'    # tail -F
```

Or SSH in and call `~/pku-court-booking-agent/scripts/webapp.sh <cmd>`
directly.

Defaults: binds `0.0.0.0:18000`, local mode (cookie not marked Secure
since we're on HTTP). Override per-invocation:

```bash
WEBAPP_HOST=127.0.0.1 WEBAPP_PORT=9999 ~/pku-court-booking-agent/scripts/webapp.sh restart
```

Pidfile and log:
- `~/pku-court-booking-agent/data/webapp.pid`
- `~/pku-court-booking-agent/data/webapp.log`

The process is detached with `setsid + nohup`, so it survives the SSH
session closing — **but it does NOT survive a server reboot.** If we
want auto-start on reboot, add a `systemd --user` unit (not done yet).

## Cloud firewall

Tencent Cloud silently filters HTTP responses on ports 80 / 443 / 8000 /
8080 / 8888 for unfiled-domain VMs on mainland — TCP handshake completes
but uvicorn never sees the request. **High ports (>10000) bypass the
filter.** Currently only ports **22** (SSH) and **18000** (webapp) are
opened in the security group. To change the webapp port, both:
1. Edit the inbound rule in the Tencent Cloud console.
2. Restart with `WEBAPP_PORT=<new>` and update the script's default.

## Configs on server

All copied via rsync from the local repo (gitignored, so they don't
travel via `git pull`). Locations:

- `config/cli/user_config.yaml` + `site_config.yaml`
- `config/webapp/auth.yaml` + `accounts.yaml`
- `config/webapp/scheduled/user_config.yaml` + `site_config.yaml`
- `config/webapp/test/user_config.yaml` + `site_config.yaml`

When changing configs locally, push the relevant file(s) up via rsync:

```bash
rsync -avz config/webapp/scheduled/user_config.yaml \
  tianze@43.173.124.100:~/pku-court-booking-agent/config/webapp/scheduled/
```

## Profiling

`profile: true` is set in
`config/webapp/scheduled/user_config.yaml` on the server (matches local),
so each scheduled run will dump a per-day JSON to
`~/pku-court-booking-agent/data/profiles/scheduled_YYYYMMDD.json`. Pull
them back with:

```bash
rsync -avz tianze@43.173.124.100:~/pku-court-booking-agent/data/profiles/ \
  data/profiles/
```

Then re-run `python scripts/profile_stats.py data/profiles --md` for
combined local+server stats.

## Updating code on the server (pull + restart)

This is the standard "deploy the latest code" flow. Push your commits to
`origin/main` locally first, then on the server:

**1. Pull the latest code:**

```bash
ssh tianze@43.173.124.100 'cd ~/pku-court-booking-agent && git pull'
```

**2. (Only if `requirements.txt` changed) reinstall deps:**

```bash
ssh tianze@43.173.124.100 'cd ~/pku-court-booking-agent && .venv/bin/pip install -r requirements.txt'
# and if a new Playwright version is pulled in:
ssh tianze@43.173.124.100 'cd ~/pku-court-booking-agent && .venv/bin/python -m playwright install chromium'
```

**3. (Optional but recommended) smoke-test the CLI before restarting.**
Note: this books REAL courts using the server's `config/cli/user_config.yaml`
(currently 2 workers: `stz` + `zy`), so each run holds 2 unpaid reservations
that you must then pay or cancel from the orders page. Use sparingly.

```bash
ssh tianze@43.173.124.100 'cd ~/pku-court-booking-agent && \
  timeout 280 .venv/bin/python main.py \
  -c config/cli/user_config.yaml --site-config config/cli/site_config.yaml 2>&1 | tail -25'
```

A non-booking sanity check that does NOT burn reservations:

```bash
ssh tianze@43.173.124.100 'cd ~/pku-court-booking-agent && \
  .venv/bin/python main.py --query-orders 3 2>&1 | tail -20'
```

**4. Restart the webapp and confirm it came back up:**

```bash
ssh tianze@43.173.124.100 '~/pku-court-booking-agent/scripts/webapp.sh restart && \
  sleep 2 && ~/pku-court-booking-agent/scripts/webapp.sh status'
# then verify the log shows "Application startup complete" + the next scheduler fire:
ssh tianze@43.173.124.100 'tail -8 ~/pku-court-booking-agent/data/webapp.log'
```

A healthy restart logs `starting webapp in local mode on 0.0.0.0:18000`,
`scheduler: next fire <date>`, and `Application startup complete`.

## Security note

This setup is **plain HTTP on port 18000**. The webapp login password
and session cookie cross the public internet unencrypted, so anyone on
the network path between your client device and the server can read
them.

What's still safe:
- The webapp password is stored on disk only as a PBKDF2 hash.
- PKU IAAA / alumni credentials never leave the server (browser talks
  directly to epe.pku.edu.cn from the server, not via your client).

What's exposed by HTTP:
- The webapp username + password whenever you log in.
- The session cookie, which lets the bearer use the dashboard until
  it expires.

Mitigations (in increasing effort):
1. Restrict the security-group inbound rule to your home IP only,
   instead of `0.0.0.0/0`. Easiest mitigation.
2. Use SSH tunnel instead of public bind:
   `ssh -L 18000:127.0.0.1:18000 tianze@43.173.124.100`, then change
   webapp default to bind 127.0.0.1.
3. Set up nginx + a TLS cert (self-signed, or Let's Encrypt if you
   point a domain at the VM), open port 443 instead of 18000, run
   webapp with `WEBAPP_MODE=remote`. Most secure.

## Quick troubleshooting

- **Dashboard unreachable from outside, but `curl localhost:18000` works
  on the server:** check Tencent Cloud security group; port 18000 must
  be open for the source IP you're connecting from.
- **`webapp.sh start` says "FAILED to start":** inspect
  `data/webapp.log` for the traceback. Common cause: missing config
  file (`accounts.yaml` not synced, etc.).
- **Scheduler fires but booking crashes with `playwright` import
  error:** `.venv` is broken; rebuild with
  `python3 -m venv .venv && .venv/bin/python -m ensurepip && .venv/bin/pip install -r requirements.txt && .venv/bin/python -m playwright install chromium`.
- **OCR / Chaojiying captcha errors:** unrelated to deployment; same
  pipeline as local. Check `captcha.username/api_key/softid` in
  `config/cli/user_config.yaml`.
