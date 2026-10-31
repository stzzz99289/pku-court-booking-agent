# Reservation date-label regression — October 31, 2026

## Root cause and evidence

The reservation page now renders date buttons as `星期日\n11/01`, rather than
the previously supported `11月01日`. Both the post-release visibility check
and date-selection function used the Chinese display string as a substring.
Consequently, a released, visible date was mistaken for an unavailable date.

- October 31: all 14 workers failed with `Date 11月01日 ... is not available
  on this page. Available: 星期六\n10/31, 星期日\n11/01`.
- Retained October 25–31 profiles: 95 of 98 worker results have this mismatch;
  the other three are login failures, not this date-selection regression.
- The local, signed-in debugging Chrome reservation page confirms `10/31`
  and `11/01` buttons, plus a separate future-release notice above them.
- The October 31 workers stopped before court selection/CAPTCHA/submission.
  This is distinct from the earlier, still-unproven invalid-CAPTCHA issue.
  Client evidence establishes the UI compatibility break, not a particular
  server-side deployment date or backend implementation change.

## Fix

`src/booking/date_labels.py` validates the configured calendar date and compares
numeric month/day components against either `MM/DD` or legacy `MM月DD日`.
Both release detection and date selection use this shared matcher. Matching
remains scoped to the actual date buttons, excluding the release notice.
Numeric boundaries prevent substring collisions such as `1/1` vs `11/01`.
The shared button selector lives in `src/booking/site_constants.py`.

No change to booking priorities, release time, retries, worker count, CAPTCHA,
or submit behavior. Recognizing an already-released date also avoids the two
unnecessary extra reloads previously triggered by the format mismatch.

## Validation and next scheduled run

Regression tests cover current/legacy labels, missing dates, strict calendar
validation, wrong-day matching, an already-selected date, release polling and
the existing three-refresh cap. An opt-in real headless Chromium fixture tests
both production functions with synthetic responses, including the day-info
cache and the absence of duplicate requests for an active date. It never
contacts the live booking site or creates an order.

Validation on October 31: all 90 tests passed with `RUN_BROWSER_TESTS=1`,
including the real Chromium date fixture and the existing CAPTCHA-observer
fixture. Live Chrome inspection was read-only; no reservation was submitted.

On November 1, the laptop should prepare at 11:57, recognize November 2's
`11/02` date after the noon release, and continue to court selection. Actual
booking success still depends on availability and subsequent website checks.
The server remains in external/display mode; only the laptop submits bookings.

Review the next report for `target date 2026-11-02 visible`, `Selecting date:
2026-11-02` (or already-selected), then normal court/CAPTCHA outcomes. If a
different failure remains, analyze that stage separately; do not label a date
format repair as proof that historical CAPTCHA errors are fixed.
