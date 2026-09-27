"""Cancel one verified mobile order, with or without a refund page."""
from __future__ import annotations

import asyncio
import re
from typing import Callable
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


async def _canceled_card_visible(card, order_no: str) -> bool:
    """Confirm the site's exact card now says canceled, not merely vanished."""
    try:
        if await card.count() != 1 or await card.get_by_text(order_no, exact=True).count() != 1:
            return False
        return bool(re.search(r"订单状态\s*已取消", await card.inner_text(timeout=1_000)))
    except Exception:
        return False


async def _wait_for_cancel_path(page, card, order_no: str, timeout_s: float = 20.0) -> str:
    """A free order can cancel in place; a paid order opens a refund page."""
    refund_path = f"/venue/orders-return-charge/{order_no}"
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if urlparse(page.url).path == refund_path:
            return "refund"
        if await _canceled_card_visible(card, order_no):
            return "direct"
        await asyncio.sleep(0.25)
    return "unconfirmed"


async def cancel_user_order(
    cfg, order_no: str,
    on_stage: Callable[[str, str], None] | None = None,
) -> str:
    """Cancel exactly ``order_no``; never retry an ambiguous final submission."""
    def stage(key: str, message: str) -> None:
        if on_stage is not None:
            on_stage(key, message)

    context, _ = await launch_persistent_context(cfg)
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        stage("login", "Checking the booking-account login")
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
        stage("logged_in", "Booking account logged in")
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
        stage("order_found", "Exact order found and confirmed active")
        cancel = card.get_by_text("取消预约", exact=True)
        if await cancel.count() != 1 or not await cancel.is_visible():
            raise OrderCancellationError("The site does not offer cancellation for this order.")

        stage("opening_cancel", "Clicking Cancel on the matching order")
        await cancel.click()
        popover = page.locator(".ivu-poptip-popper:visible")
        if await popover.count() != 1 or "您确认取消这条预约吗" not in await popover.inner_text():
            raise OrderCancellationError("The site's cancellation confirmation was unexpected.")
        confirm = popover.get_by_text("确定", exact=True)
        if await confirm.count() != 1:
            raise OrderCancellationError("The site's cancellation confirmation was ambiguous.")
        stage("confirming", "Confirming the booking site's cancel prompt")
        await confirm.click()
        stage("verifying", "Checking whether the site canceled directly or opened refund details")
        path = await _wait_for_cancel_path(page, card, order_no)
        if path == "direct":
            stage("completed", "Booking site shows this order as canceled")
            return "The booking site confirmed cancellation of this order."
        if path == "unconfirmed":
            # Re-open the record once: no second cancellation click or submit.
            await _goto_with_retry(page, _mobile_orders_url(cfg.base_url))
            refreshed = await _find_mobile_card(page, order_no)
            if refreshed is not None and await _canceled_card_visible(refreshed, order_no):
                stage("completed", "Booking site shows this order as canceled")
                return "The booking site confirmed cancellation of this order."
            raise OrderCancellationError(
                "Cancellation outcome is unconfirmed. Check the booking site before retrying."
            )

        stage("refund_details", "Refund details opened for the exact order")
        expected_path = f"/venue/orders-return-charge/{order_no}"
        if urlparse(page.url).path != expected_path:
            raise OrderCancellationError("The final cancellation page did not identify the exact order.")
        try:
            await page.get_by_text(order_no, exact=True).wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError("The final cancellation page did not identify the exact order.") from exc
        if await page.get_by_text(order_no, exact=True).count() != 1:
            raise OrderCancellationError("The final cancellation page showed an ambiguous order ID.")
        submit = page.get_by_role("button", name="提交", exact=True)
        try:
            await submit.wait_for(state="visible", timeout=10_000)
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError("The final cancellation button was unavailable.") from exc
        if await submit.count() != 1:
            raise OrderCancellationError("The final cancellation button was unavailable.")

        # This is the irreversible step. A timeout afterward is not retried.
        stage("submitting", "Submitting the site's refund/cancellation form")
        await submit.click()
        stage("verifying", "Checking the site's cancellation result")
        try:
            await page.get_by_text(re.compile(r"您已成功取消.*预约订单")).wait_for(
                state="visible", timeout=20_000,
            )
        except PlaywrightTimeoutError as exc:
            raise OrderCancellationError(
                "Cancellation outcome is unconfirmed. Check the booking site before retrying."
            ) from exc
        stage("completed", "Booking site confirmed cancellation")
        return "The booking site confirmed cancellation; any refund follows its displayed policy."
    finally:
        await dispose_context(context)
