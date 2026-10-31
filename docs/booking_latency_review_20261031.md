# Booking latency review — October 31, 2026

Reviewed CLI/runner, browser/session startup, configuration and scheduler/sync,
date/court selection, CAPTCHA providers, payment handling, and profiling/logging.
No live booking was created for this review.

## Implemented

1. **One reservation navigation per transient retry.** Previously the transient
   branch navigated immediately, then the next iteration navigated again before
   date selection. Remove the first navigation; retain forced re-login when
   needed, the existing retry caps, and the next iteration's readiness checks.
   This removes one complete page/data reload (including its fixed 1.5-second
   login-modal probe) on each such retry. Historical October 11 profiles average
   about 6.1 seconds per reservation re-navigation under contention; this is
   context, not a measured saving for the next run.
2. **One atomic court-label read.** Read all first-cell labels in a single browser
   call, preserving actual row positions including placeholders and header rows.
   Reuse labels for the JSON-cache fast path and DOM fallback instead of reading
   every label individually and rereading the selected label. Court priority,
   availability-class checks on the DOM path, and normal Playwright clicks are
   unchanged. This reduces browser IPC operations; real-world milliseconds saved
   have not been measured.

## Deliberately unchanged

- The preceding date-format fix already removes unnecessary release refreshes.
- Browser launch, login/OCR and pagination discovery occur before noon; their
  duration is generally preparation cost rather than noon-to-booking latency.
- Historical profiles show release-time reloads and the CAPTCHA service as major
  remaining delays. Decreasing safety waits, increasing concurrency or removing
  worker staggering without a controlled run risks throttling or stale state.
- Payment currently waits for network idle before reading an amount that starts
  at zero. Replacing that wait needs a verified data-loaded signal; removing it
  based on visible text alone could misclassify a paid booking as free.
- The final free-payment delay also deserves a separate confirmation/readiness
  investigation. Do not simply shorten it or assume a click proves acceptance.
- Synchronous logging and startup OCR can be improved separately, but neither
  justifies dropping crash evidence or changing request timing in this small fix.

## Validation / next run

Tests assert exactly one retry navigation with and without forced re-login.
An offline real-Chromium fixture covers placeholder/header rows, cache indices,
out-of-range fallback and DOM court priorities. Existing regression tests remain
in place. All browser fixtures are synthetic and cannot reserve real courts.

Compare subsequent profiling spans `post_fire_refresh`, `select_booking_date`,
`select_court_time(cache)`, `renavigate_to_reservation`, `solve_booking_captcha`
and `confirm_payment`. Recent retained runs stopped at the date-format error,
so they cannot provide a valid full-flow before/after speed comparison.
