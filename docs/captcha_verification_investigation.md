# CAPTCHA verification investigation — October 12/13 scheduled runs

Status: **root cause not confirmed**. Prepared on 2026-10-11; updated on
2026-10-12 (Beijing time). October 12 did not execute a booking run; the next
dated observation is enabled for October 13.
This is a temporary observation experiment, not a CAPTCHA bypass or a change
to the site's verification, booking priorities, retries, or worker concurrency.

## 1. Evidence so far

The normal sequence is `captcha/get` → `captcha/check` →
`reservation/order/submit`. Checking the characters and authorizing a booking
are separate operations. The website frontend generates the booking request;
our automation clicks its controls, rather than constructing CAPTCHA tokens.

On October 11, 14 workers finished: four confirmed bookings, four unavailable
court results, six final CAPTCHA rate-limit results. Four intermediate invalid
verification incidents were captured at approximately 12:01:11 (twice),
12:01:13, and 12:01:21. These are not six invalid-verification failures.

Each captured incident shows the widget saying `验证成功`, a check response
with `data.success: true`, then an order-submit response with business code 250
and `验证码非法校验`. One check/submit response pair was 33 ms apart. That
narrows the failure to the transition from checking the challenge to verifying
the booking; it does **not** establish that coordinates, tokens, or sessions
are correct in every detail.

`验证码次数超出限制` is different: the site has throttled CAPTCHA activity.
Today's workers also retried unpaid-order and same-time booking conflicts.
Those retries and same-account concurrency plausibly increase throttling;
the site's actual threshold and whether it is account/session/IP scoped are
unknown. Reducing those retries is a separate fix, deliberately not bundled
with this observation experiment.

Existing evidence lives in `debugging/captcha_failures/20261011/` and
`debugging/post-submit/20261011/`. The baseline run log, metadata, and profile
are copied into `debugging/investigations/captcha-verification/20261011/`
before tomorrow overwrites `data/scheduled_last_run.*`.

### Why existing diagnostics cannot establish the cause

- They omit generic `token`, nested `data.repData`, `repCode`, and `repMsg`.
- Only booking `captchaToken`/`captchaVerification` have fingerprints, so
  challenge → check → booking identity cannot be compared.
- `request.headers` omits security-related headers; cookie/authentication
  continuity is therefore unproven. Use `await request.all_headers()`.
- Each failure uses a different HMAC key, preventing comparisons between
  retries and workers. Only failed attempts are saved.
- Only response handles from a single booking attempt are collected; failed
  network requests and repeated submissions over the whole run can be missed.
- The site's actual authentication headers are `cgAuthorization` and
  `cgappauthorization`, identified in its current public AJAX helper. They
  were not included in the old header allowlist at all. Challenge requests
  also carry `clientUid` from `localStorage.point`; copied worker profiles
  might share it. Log its fingerprint to test that hypothesis, not assume it
  causes token invalidation.

Reference: [Playwright request headers and events](https://playwright.dev/python/docs/api/class-request).

## 2. Temporary debugging commit and activation

Implementation: `src/booking/captcha_observation.py`, integrated into the
shared scheduler and runner. Enable it by placing this private marker at
`debugging/investigations/captcha-verification/20261012/enable.json`:

```json
{"enabled": true, "run_date": "20261012"}
```

The scheduler checks the **Beijing run date** and only that date's marker.
There is no environment-variable dependency, so activation survives reboot.
No marker means no extra capture. October 13 is automatically disabled unless
explicitly enabled with a new dated marker. CLI smoke tests are not enabled.

One-time laptop preparation:

```powershell
.venv/Scripts/python.exe -m pip install -r requirements-debug.txt
.venv/Scripts/python.exe -X utf8 scripts/prepare_captcha_study.py 20261012
```

The current public chunk is `chunk-c3869bc6.1e68c5f2.js`, SHA-256
`d4adb3cc71810fc1c7501b76134c2034acca468b1d985c777c2861adf969f4a0`.
Its VerifyPoints component encrypts the point JSON with AES-ECB/PKCS7 when a
challenge-specific key is present; booking verification encrypts
`token + "---" + the same point JSON`. It emits that verification and the token
to the reservation form. When no key is present, it emits plaintext instead.
The observer recomputes this **after the worker outcome**, compares values,
and persists only booleans/error classes. Public JS source is retained as a
study artifact; live challenge keys and decrypted points are never persisted.
This proves a frontend-generation invariant, not server acceptance. A changed
chunk path makes the study stale and requires a fresh inspection.

Capture requirements:

1. One unsaved, random HMAC key shared by this scheduled run's workers.
   Fingerprint challenge tokens, encrypted point payloads, verification values,
   and allowlisted session headers/cookies. Never save raw secrets or the key.
   Include the site's `cgAuthorization`/`cgappauthorization` and challenge
   `clientUid` fingerprints; copied local-storage identity is distinct from
   account identity and session identity.
2. Allowlisted scalar business statuses, nested `repData` fingerprints, field
   presence/length, request identity, worker index, attempt number, account
   fingerprint, target date/slot, request/response/finish timings, and failures.
3. Observe **all** relevant CAPTCHA/booking attempts, including successful
   ones and rate-limit responses. Detect duplicate booking submissions of a
   token and overlapping requests across workers using the shared fingerprints.
4. No routing/interception, additional booking requests, screenshots on success,
   full HAR/trace, raw request bodies, or authentication values. Bounded memory
   and bounded asynchronous response/header reads; summaries only. Flush the
   redacted combined JSON **after all workers finish**. Diagnostic failures must
   never crash a booking worker.
5. Inspect the public frontend's challenge/check/verification-generation code
   before the run; keep the dated asset hashes and study findings privately.
   Generation checks must follow that inspected code, not an assumed algorithm.
   No live encryption key or decrypted coordinate payload is persisted.

Outputs under the dated investigation folder:

- `run_<id>.json`: complete bounded redacted network summaries and per-worker
  final outcomes, including successful comparisons; capture errors/truncation
  are explicit, not silently interpreted as missing website requests.
- `site-study/`: public asset snapshot/hashes and verification-generation study.
- `README.md`: private landing instructions and checklist.
- Existing failure images remain under `captcha_failures/YYYYMMDD/`; full-page
  failures land in `post-submit/YYYYMMDD/` with microsecond filenames.

Commit scope is observation + tests + docs + diagnostic output organization.
Do not change private booking config, register a second task, run another real
booking, or deploy laptop-only diagnostics to the display server. The existing
11:57 Windows task reads the repo code automatically. Verify it stays enabled.

## 3. Expected next-run results and analysis

Run date: **2026-10-12**. Preparation 11:57, release 12:00; laptop must be awake
and signed into Windows. Locked desktop is fine. Results appear only after
workers finish; normal five-minute server-report cooldown remains unchanged.

First validate capture health, worker coverage, and absence of truncation.
Sort by request-start time, then group by worker/attempt and shared challenge
token fingerprint. Compare failed examples against successful examples.

Quick read-only summary (replace `<id>` with the actual filename):

```powershell
.venv/Scripts/python.exe -X utf8 scripts/analyze_captcha_observation.py debugging/investigations/captcha-verification/20261012/run_<id>.json
```

The summary counts intermediate invalid/rate-limit submissions separately from
final worker outcomes and flags repeated tokens, shared CAPTCHA client IDs,
asset mismatch and incomplete capture. Those flags are observations, not an
automatic root-cause verdict. Follow up in the full trace for session continuity
and exact cross-worker request order.

| Observation | Interpretation / next action |
| --- | --- |
| Issued, checked, and booked challenge tokens differ | Establish stale/mixed-token client flow; locate refresh/overlap that changed it. |
| Same token submitted more than once | Establish reuse; inspect the first response and event ordering before considering another submission safe. |
| Check business status fails despite a success-looking widget | Fix acceptance detection using the actual business outcome. |
| Session fingerprints change between check and booking | Investigate login/session turnover; cookie ordering differences alone are not proof. |
| Verification generation differs from the inspected frontend algorithm | Investigate stale widget/form state and the exact emitted verification value. |
| Multiple workers overlap, but no identity mismatch or reuse | Concurrency is a hypothesis, not proof; consider a later controlled one-worker comparison. |
| All invariants match, but site returns code 250 | Preserve exact timestamps, statuses, asset version and sanitized server request IDs; server-side expiry/consumption/backend problems remain possible. Website logs may be required. |
| No invalid verification occurs | Useful success baseline, **not** proof that the bug is fixed. Re-enable a dated capture if it recurs. |
| Only rate-limit errors occur | Study shared submission counts/timing; do not relabel as invalid verification or sold-out. |

Never conclude that the CAPTCHA token is accepted simply because HTTP status
is 200. Never claim definitive server internals from client-only evidence.

## 4. Preparation checkpoint (October 11)

- Private date marker installed for October 12 only; October 13 verified off.
- Public frontend snapshot downloaded and its generation algorithm inspected;
  optional local encryption library installed without upgrading Playwright.
- Offline regression suite passed (79 tests, one opt-in test skipped), and the
  opt-in real headless Chromium test passed separately with entirely synthetic
  responses. It makes no requests to the live booking site.
- Existing daily Windows task is enabled and Ready; next preparation is
  October 12 at 11:57. No extra task or booking was launched.
- Today’s baseline log/metadata/profile retained; obsolete smoke/login/lock
  artifacts deleted, their useful study scripts retained. Unresolved crashes,
  CAPTCHA images, and full-page failure evidence preserved and categorized.
- No private booking/account config changed; no server deployment required.

## 5. October 12 findings — no booking executed

Checked at approximately 23:47 Beijing time on October 12:

- The daily Windows task is enabled and Ready. LastRunTime is October 12
  **12:01:12**, LastTaskResult is **2**, and the next trigger is October 13
  **11:57:00**. The configuration is still 12:00 release / 180-second prep,
  with 14 workers. No booking config or trigger was changed during this check.
- `local_schedule.run_once()` returns 2 before starting the scheduler when
  invoked outside the preparation window. It deliberately refuses to submit
  late bookings. The current local task log also says:
  `Outside the scheduled preparation window; no booking was attempted.`
- That log's latest write is 13:58:47; the entry point truncates it on every
  invocation. Therefore it is not a complete history and cannot establish why
  the task was late or whether a later manual invocation replaced its log.
  Do not claim that sleep/sign-out caused this without additional OS evidence.
- Local `scheduled_last_run.json` and `.log` are still October 11's, as is the
  server's report `started_at` (1791691021.3968258). A newer server file write
  is not evidence of a new booking: routine sync can republish the old report.
- No October 12 `run_*.json` exists in the investigation folder. Thus no new
  CAPTCHA request chain, successful comparison, or invalid-verification result
  exists to analyze. The invalid-verification root cause remains unconfirmed.

The unsuccessful invocation's log/status are preserved privately under
`debugging/investigations/captcha-verification/20261012/`. No live booking was
launched to compensate for the missed run.

## 6. October 13 follow-up — enabled, results pending

The same passive diagnostics are enabled by the new private marker:

```text
debugging/investigations/captcha-verification/20261013/enable.json
```

Its contents are `{"enabled": true, "run_date": "20261013"}`. A fresh public
asset study is saved in that date's `site-study/` folder. No code changes,
extra task, retry/concurrency changes, or server restart are required.
The existing daily task loads the observer automatically. October 14 remains
disabled unless explicitly re-enabled with another dated marker.

For October 13, keep the laptop awake and signed into Windows **before 11:57**;
Win+L is fine, but sleep/sign-out is not. The current task neither wakes the
laptop nor catches up on missed runs. Use the same analysis checklist in
section 3, substituting `20261013` in the diagnostic paths.

- Actual preparation / finish / upload time: **pending**
- `run_<id>.json` and capture completeness: **pending**
- Worker outcomes vs intermediate CAPTCHA business errors: **pending**
- Successful/failed token, client-ID, session and generation comparison: **pending**
- Evidence-supported cause, or exact remaining uncertainty: **pending**
- Narrow fix / validation / capture removal: **pending**

The scheduler does not edit this tracked document. Fill this section during
human-requested analysis after the run. Diagnostics remain local and ignored
by Git; only the investigation plan and reusable code/tests are committed.
