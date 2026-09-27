from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from src.booking.site_constants import MOBILE_ORDER_CARD_SELECTOR
from web.backend.order_cancellation import OrderCancellationError, cancel_user_order


ORDER_NO = "D260927000140"


class OrderCancellationTests(unittest.IsolatedAsyncioTestCase):
    def _page(self, *, exact_card: bool = True):
        page = MagicMock()
        page.url = "https://epe.pku.edu.cn/venue/mobileOrders"
        page.set_viewport_size = AsyncMock()
        page.wait_for_url = AsyncMock()
        cards = MagicMock()
        cards.first.wait_for = AsyncMock()
        popover = MagicMock()
        popover.count = AsyncMock(return_value=1)
        popover.inner_text = AsyncMock(return_value="您确认取消这条预约吗？ 取消 确定")
        page.locator.side_effect = lambda selector: (
            cards if selector == MOBILE_ORDER_CARD_SELECTOR else popover
        )

        order_label = MagicMock()
        order_label.count = AsyncMock(return_value=1 if exact_card else 0)
        cancel = MagicMock()
        cancel.count = AsyncMock(return_value=1)
        cancel.is_visible = AsyncMock(return_value=True)
        cancel.click = AsyncMock()
        card = MagicMock()
        card.get_by_text.side_effect = lambda text, **_kwargs: (
            order_label if text == ORDER_NO else cancel
        )
        card.inner_text = AsyncMock(return_value=(
            f"订单号\n{ORDER_NO}\n订单状态\n正常\n支付状态\n已支付\n取消预约"
        ))

        confirm = MagicMock()
        confirm.count = AsyncMock(return_value=1)

        async def confirm_click():
            page.url = f"https://epe.pku.edu.cn/venue/orders-return-charge/{ORDER_NO}"

        confirm.click = AsyncMock(side_effect=confirm_click)
        popover.get_by_text.return_value = confirm

        success = MagicMock()
        success.wait_for = AsyncMock()
        page.get_by_text.side_effect = lambda text, **_kwargs: (
            order_label if text == ORDER_NO else success
        )
        submit = MagicMock()
        submit.count = AsyncMock(return_value=1)
        submit.is_visible = AsyncMock(return_value=True)
        submit.click = AsyncMock()
        page.get_by_role.return_value = submit
        return page, card, submit

    async def test_submits_once_only_after_exact_order_checks(self) -> None:
        page, card, submit = self._page()
        context = SimpleNamespace(pages=[page])
        cfg = SimpleNamespace(base_url="https://epe.pku.edu.cn/venue/home")
        with (
            patch("web.backend.order_cancellation.launch_persistent_context",
                  new_callable=AsyncMock, return_value=(context, Path("unused"))),
            patch("web.backend.order_cancellation.dispose_context", new_callable=AsyncMock),
            patch("web.backend.order_cancellation._goto_with_retry", new_callable=AsyncMock),
            patch("web.backend.order_cancellation.ensure_logged_in", new_callable=AsyncMock),
            patch("web.backend.order_cancellation._orders_session_rejected",
                  new_callable=AsyncMock, return_value=False),
            patch("web.backend.order_cancellation._find_mobile_card",
                  new_callable=AsyncMock, return_value=card),
            patch("web.backend.order_cancellation._default_login_solver", return_value=object()),
        ):
            message = await cancel_user_order(cfg, ORDER_NO)
        self.assertIn("confirmed cancellation", message)
        submit.click.assert_awaited_once()

    async def test_wrong_order_never_reaches_final_submission(self) -> None:
        page, card, submit = self._page(exact_card=False)
        context = SimpleNamespace(pages=[page])
        cfg = SimpleNamespace(base_url="https://epe.pku.edu.cn/venue/home")
        with (
            patch("web.backend.order_cancellation.launch_persistent_context",
                  new_callable=AsyncMock, return_value=(context, Path("unused"))),
            patch("web.backend.order_cancellation.dispose_context", new_callable=AsyncMock),
            patch("web.backend.order_cancellation._goto_with_retry", new_callable=AsyncMock),
            patch("web.backend.order_cancellation.ensure_logged_in", new_callable=AsyncMock),
            patch("web.backend.order_cancellation._orders_session_rejected",
                  new_callable=AsyncMock, return_value=False),
            patch("web.backend.order_cancellation._find_mobile_card",
                  new_callable=AsyncMock, return_value=card),
            patch("web.backend.order_cancellation._default_login_solver", return_value=object()),
        ):
            with self.assertRaises(OrderCancellationError):
                await cancel_user_order(cfg, ORDER_NO)
        submit.click.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
