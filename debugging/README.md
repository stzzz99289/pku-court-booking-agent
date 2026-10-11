# Debugging workspace

This directory is for private local investigation artifacts. Its contents
are ignored by Git except for this guide.

- `investigations/captcha-verification/YYYYMMDD/` — dated experiment markers,
  complete redacted scheduled-run network evidence, baseline logs and public
  asset studies. See `docs/captcha_verification_investigation.md`.
- `captcha_failures/YYYYMMDD/` — rejected challenge images + geometry/status evidence
- `post-submit/YYYYMMDD/` — HTML, JSON, and full-page screenshots from booking failures
- `crashes/YYYYMMDD/` — automatic pre-disposal evidence for unexpected worker exceptions
- `experiments/smoke/YYYYMMDD/` — future disposable smoke-test captures
- `experiments/probes/YYYYMMDD/` — future focused diagnostic probes
- `studies/` — public site assets, UI studies, and preserved legacy helper scripts
- `samples/` — manually supplied reference images

Production order-proof screenshots do **not** belong here. The webapp stores
them privately under `data/order_proofs/`, which is also ignored by Git and is
maintained automatically.

Do not dump new files in this directory's root. Do not prune unresolved crash,
CAPTCHA or post-submit evidence automatically. Keep the latest scheduled-run
files in `data/`; copy a baseline into a dated investigation before it is
overwritten. Scheduled profiles in `data/profiles/` retain their existing
seven-day window. Old disposable smoke/login/lock-test artifacts were removed
on 2026-10-11; relevant study code/assets were retained. Nothing here is
uploaded to the display server or committed, except this guide.
