# Blank order details — 2026-11-02

## Evidence and root cause

- Today's scheduled booking confirmed two new orders. Both were present in
  the server's order cache after manual refreshes, so booking and payment were
  not the cause of the empty dashboard.
- The cache had 39 orders, all with blank `venue` and `court_and_time` fields.
  The default exact-match venue filter correctly excluded these blank values.
- A read-only inspection of the live desktop `/venue/orders` page confirmed
  that its venue and court/time `<td>` elements were genuinely empty, not
  hidden by CSS. The table headers and remaining columns were unchanged.
- The same page's `/venue-server/api/orders/mine` response still included
  `tradeNo`, `venueName`, `siteName`, and `reservationDateDetail`. The mobile
  card correctly displayed the venue and court/time for the same order ID.

This is a desktop rendering/data-contract regression on the PKU site, not
evidence that reservations disappeared or that the venue was renamed. We
cannot establish which upstream release introduced it from these observations.

## Fix

Keep DOM-derived payment/status labels, order numbers, dates, and pagination.
Passively observe the browser's existing same-origin GET order-list responses;
retain only the exact order ID and the two required display fields. Supplement
only blank DOM fields, joining by the complete order ID, never row position.
Concatenate `venueName` and `siteName` to retain the historical dashboard filter
value. Enrich orders before proof capture and cache persistence.

If a normal paid order is still missing either detail, fail that user's refresh
instead of silently publishing unfilterable orders. The existing cache service
preserves the previous result and shows a refresh warning. No new requests,
booking submissions, or cancellation actions are introduced. No raw API body,
phone number, account ID, or authentication data is logged or persisted.

## Validation

- Unit coverage: exact origin/path/GET/status guards; malformed responses;
  allowlisted display fields; exact ID matching with reordered IDs; preserving
  existing DOM text; rejecting incomplete active orders.
- Offline real-Chromium regression: blank desktop cells plus reversed API row
  order are repaired by the production fetch flow. Production dashboard JS
  displays both cards with the unchanged default venue filter. Only the three
  expected GETs occur (home, orders, and the page's own order-list request).
- Production acceptance: deploy, refresh all users through the existing
  server-side refresh job, verify the two new orders have nonempty venue and
  court/time fields and match the default filter. No live booking is required.
