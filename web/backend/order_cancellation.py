"""Cancel one verified mobile order through the site's refund flow."""
from __future__ import annotations

import re
from urllib.parse import urlparse

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from src.booking.browser import dispose_context, launch_persistent_context
from src.booking.login import ensure_logged_in
from src.booking.orders import (
    _default_login_solver, _dismiss_visible_confirm, _goto_with_retry,
    _orders_session_rejected,
)
from src.booking.site_constants import MOBILE_ORDER_CARD_SELECTOR
from web.backend.order_proofs import _find_mobile_card, _mobile_orders_url


class OrderCancellationError(RuntimeError):
    """A cancellation could not be safely completed or confirmed."""


async def cancel_user_order(cfg, order_no: str) -> str:
    """Cancel exactly ``order_no``; never retry an ambiguous final submission."""
    context, _ = await launch_persistent_context(cfg)
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        await _goto_with_retry(page, cfg.base_url)
        await ensure_logged_in(page, cfg, _default_login_solver())
        await page.set_viewport_size({"width": 390, "height": 844})
        await _goto_with_retry(page, _mobile_orders_url(cfg.base_url))
        if await _orders_session_rejected(page):
            await _dismiss_visible_confirm(page)
            await ensure_logged_in(page, cfg, _default_login_solver(), force=True)
            await _goto_with_retry(page, _mobile_orders_url(cfg.base_url))
            if await _orders_session_rejected(page):
                raise OrderCancellationError("The booking site rejected this account's login.")
        try:
            await page.locator(MOBILE_ORDER_CARD_SELECTOR).first.wait_for(
                state="visible", timeout=10_000,
            )
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError("The booking site's order cards did not load.") from exc
        card = await _find_mobile_card(page, order_no)
        if card is None or await card.get_by_text(order_no, exact=True).count() != 1:
            raise OrderCancellationError("The exact order was not found in this account's records.")
        text = await card.inner_text()
        if not re.search(r"订单状态\s*正常", text) or not re.search(r"支付状态\s*已支付", text):
            raise OrderCancellationError("The order is no longer active and paid; nothing was canceled.")
        cancel = card.get_by_text("取消预约", exact=True)
        if await cancel.count() != 1 or not await cancel.is_visible():
            raise OrderCancellationError("The site does not offer cancellation for this order.")

        await cancel.click()
        popover = page.locator(".ivu-poptip-popper:visible")
        if await popover.count() != 1 or "您确认取消这条预约吗" not in await popover.inner_text():
            raise OrderCancellationError("The site's cancellation confirmation was unexpected.")
        confirm = popover.get_by_text("确定", exact=True)
        if await confirm.count() != 1:
            raise OrderCancellationError("The site's cancellation confirmation was ambiguous.")
        await confirm.click()

        expected_path = f"/venue/orders-return-charge/{order_no}"
        try:
            await page.wait_for_url(re.compile(rf"/venue/orders-return-charge/{re.escape(order_no)}(?:[?#]|$)"), timeout=15_000)
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError("The site did not open the final cancellation page.") from exc
        if urlparse(page.url).path != expected_path or await page.get_by_text(order_no, exact=True).count() != 1:
            raise OrderCancellationError("The final cancellation page did not identify the exact order.")
        submit = page.get_by_role("button", name="提交", exact=True)
        if await submit.count() != 1 or not await submit.is_visible():
            raise OrderCancellationError("The final cancellation button was unavailable.")

        # This is the irreversible step. A timeout afterward is not retried.
        await submit.click()
        try:
            await page.get_by_text(re.compile(r"您已成功取消.*预约订单")).wait_for(
                state="visible", timeout=20_000,
            )
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError(
                "Cancellation outcome is unconfirmed. Check the booking site before retrying."
            ) from exc
        return "The booking site confirmed cancellation; any refund follows its displayed policy."
    finally:
        await dispose_context(context)
