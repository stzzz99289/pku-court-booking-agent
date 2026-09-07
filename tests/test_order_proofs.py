from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from src.booking.orders import Order
from web.backend.order_proofs import (
    OrderProofStore,
    _assert_cjk_font_available,
    capture_missing_order_proofs,
)


def _order(order_no: str, use_date: str) -> Order:
    return Order(
        user="zy",
        order_no=order_no,
        order_type="在线预约",
        venue="五四体育中心室外网球场",
        use_date=use_date,
        court_and_time="1号场 20:00-21:00",
        pay_status="已支付",
        order_status="正常",
        amount="40",
        created_at="2026-09-06 12:00:00",
    )


class OrderProofStoreTests(unittest.TestCase):
    def test_stable_url_and_lookup_survive_new_store_instance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = OrderProofStore(root)
            order = _order("D/unsafe?260907", "2026-09-07")
            path = store.target_path(order.user, order.order_no)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"png")
            store.record(order, path)

            reopened = OrderProofStore(root)
            self.assertEqual(reopened.cached_path(order.user, order.order_no), path)
            self.assertIn("order_no=D%2Funsafe%3F260907", reopened.proof_url("zy", order.order_no))
            decorated = reopened.attach_urls([order.to_dict()])
            self.assertEqual(decorated[0]["proof_url"], reopened.proof_url("zy", order.order_no))

    def test_prune_removes_only_past_proofs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OrderProofStore(Path(directory))
            orders = [
                _order("PAST", "2026-09-06"),
                _order("TODAY", "2026-09-07"),
                _order("FUTURE", "2026-09-08"),
            ]
            paths: dict[str, Path] = {}
            for order in orders:
                path = store.target_path(order.user, order.order_no)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"png")
                store.record(order, path)
                paths[order.order_no] = path

            self.assertEqual(store.prune_past(date(2026, 9, 7)), 1)
            self.assertFalse(paths["PAST"].exists())
            self.assertTrue(paths["TODAY"].exists())
            self.assertTrue(paths["FUTURE"].exists())


class OrderProofCaptureTests(unittest.IsolatedAsyncioTestCase):
    def test_linux_capture_rejects_missing_chinese_fonts(self) -> None:
        with (
            patch("web.backend.order_proofs.sys.platform", "linux"),
            patch("web.backend.order_proofs.shutil.which", return_value="fc-list"),
            patch(
                "web.backend.order_proofs.subprocess.run",
                return_value=SimpleNamespace(returncode=0, stdout=b""),
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "no Chinese font"):
                _assert_cjk_font_available()

    async def test_missing_proof_is_captured_from_mobile_card(self) -> None:
        class Card:
            async def wait_for(self, **_kwargs):
                return None

            async def screenshot(self, *, path, **_kwargs):
                Path(path).write_bytes(b"png")

        class Locator:
            def __init__(self, card):
                self.first = card

            def filter(self, **_kwargs):
                return self

            async def count(self):
                return 1

        class Page:
            def __init__(self):
                self.viewport_size = {"width": 1280, "height": 720}
                self.card = Card()
                self.visited = None

            async def set_viewport_size(self, viewport):
                self.viewport_size = viewport

            async def goto(self, url, **_kwargs):
                self.visited = url

            async def add_style_tag(self, **_kwargs):
                return None

            async def evaluate(self, _expression):
                return None

            def locator(self, _selector):
                return Locator(self.card)

        with tempfile.TemporaryDirectory() as directory:
            store = OrderProofStore(Path(directory))
            order = _order("NEW", "2026-09-08")
            page = Page()

            with patch("web.backend.order_proofs._assert_cjk_font_available"):
                stats = await capture_missing_order_proofs(
                    page,
                    "https://epe.pku.edu.cn/venue/home",
                    [order],
                    store,
                    today=date(2026, 9, 7),
                )

            self.assertEqual(stats, {"captured": 1, "cached": 0, "failed": 0})
            self.assertEqual(page.visited, "https://epe.pku.edu.cn/venue/mobileOrders")
            self.assertEqual(page.viewport_size, {"width": 1280, "height": 720})
            self.assertIsNotNone(store.cached_path("zy", "NEW"))

    async def test_cached_proof_does_not_touch_browser_page(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = OrderProofStore(Path(directory))
            order = _order("CACHED", "2026-09-08")
            path = store.target_path(order.user, order.order_no)
            path.parent.mkdir(parents=True)
            path.write_bytes(b"png")
            store.record(order, path)

            stats = await capture_missing_order_proofs(
                object(),
                "https://epe.pku.edu.cn/venue/home",
                [order],
                store,
                today=date(2026, 9, 7),
            )

            self.assertEqual(stats, {"captured": 0, "cached": 1, "failed": 0})


if __name__ == "__main__":
    unittest.main()
