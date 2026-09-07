"""Capture and maintain private proof screenshots for paid court orders."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

from playwright.async_api import Page

from src.booking.orders import Order
from src.booking.site_constants import MOBILE_ORDER_CARD_SELECTOR, MOBILE_ORDERS_PATH

log = logging.getLogger(__name__)

PROOF_VIEWPORT = {"width": 390, "height": 844}
PAID_STATUS = "已支付"
NORMAL_STATUS = "正常"
_LOAD_MORE_TEXT = "加载更多"


def _assert_cjk_font_available() -> None:
    """Fail before caching tofu-box screenshots on Linux hosts."""
    fc_list = shutil.which("fc-list")
    if not sys.platform.startswith("linux") or fc_list is None:
        return
    try:
        result = subprocess.run(
            [fc_list, ":lang=zh"],
            check=False,
            capture_output=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"could not inspect installed fonts: {exc}") from exc
    if result.returncode != 0 or not result.stdout.strip():
        raise RuntimeError(
            "no Chinese font is installed; install fonts-noto-cjk before "
            "capturing order proofs"
        )


def _date_value(value: str) -> date | None:
    digits = re.sub(r"\D", "", value)[:8]
    try:
        return datetime.strptime(digits, "%Y%m%d").date()
    except (TypeError, ValueError):
        return None


def _safe_component(value: str) -> str:
    """Return a readable, collision-resistant filename component."""
    readable = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._") or "item"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{readable[:48]}-{digest}"


class OrderProofStore:
    """Disk-backed mapping from ``(user, order_no)`` to private PNG files."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.index_file = root / "index.json"

    @staticmethod
    def _key(user: str, order_no: str) -> str:
        return f"{user}\0{order_no}"

    def _load_index(self) -> dict[str, dict[str, str]]:
        if not self.index_file.is_file():
            return {}
        try:
            raw = json.loads(self.index_file.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise ValueError("proof index must be an object")
            return {
                str(key): value
                for key, value in raw.items()
                if isinstance(value, dict)
                and all(isinstance(value.get(field), str) for field in (
                    "user", "order_no", "use_date", "file",
                ))
            }
        except Exception as exc:
            log.warning("order proofs: could not read %s: %s", self.index_file, exc)
            return {}

    def _write_index(self, index: dict[str, dict[str, str]]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.index_file.with_suffix(".tmp")
        temp.write_text(json.dumps(index, indent=2, ensure_ascii=False), encoding="utf-8")
        try:
            os.chmod(temp, 0o600)
        except OSError:
            pass
        temp.replace(self.index_file)

    def target_path(self, user: str, order_no: str) -> Path:
        return (
            self.root
            / _safe_component(user)
            / f"{_safe_component(order_no)}.png"
        )

    def cached_path(self, user: str, order_no: str) -> Path | None:
        entry = self._load_index().get(self._key(user, order_no))
        if not entry:
            return None
        try:
            root = self.root.resolve()
            candidate = (root / entry["file"]).resolve()
            if root not in candidate.parents or not candidate.is_file():
                return None
            return candidate
        except (KeyError, OSError, RuntimeError):
            return None

    def record(self, order: Order, path: Path) -> None:
        index = self._load_index()
        relative = path.resolve().relative_to(self.root.resolve()).as_posix()
        index[self._key(order.user, order.order_no)] = {
            "user": order.user,
            "order_no": order.order_no,
            "use_date": order.use_date,
            "file": relative,
        }
        self._write_index(index)

    def proof_url(self, user: str, order_no: str) -> str | None:
        if self.cached_path(user, order_no) is None:
            return None
        return (
            f"/api/orders/proof?user={quote(user, safe='')}"
            f"&order_no={quote(order_no, safe='')}"
        )

    def attach_urls(self, orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
        decorated: list[dict[str, Any]] = []
        for order in orders:
            item = dict(order)
            item["proof_url"] = self.proof_url(
                str(item.get("user", "")), str(item.get("order_no", "")),
            )
            decorated.append(item)
        return decorated

    def prune_past(self, today: date | None = None) -> int:
        """Remove cached proofs whose recorded use date is before ``today``."""
        today = today or date.today()
        index = self._load_index()
        kept: dict[str, dict[str, str]] = {}
        removed = 0
        for key, entry in index.items():
            proof_date = _date_value(entry["use_date"])
            root = self.root.resolve()
            path = (root / entry["file"]).resolve()
            inside_root = root in path.parents
            expired = proof_date is not None and proof_date < today
            if not inside_root or expired or not path.is_file():
                if inside_root and path.is_file():
                    try:
                        path.unlink()
                    except OSError as exc:
                        log.warning("order proofs: could not remove %s: %s", path, exc)
                        kept[key] = entry
                        continue
                removed += 1
            else:
                kept[key] = entry
        if kept != index:
            self._write_index(kept)
        for directory in self.root.iterdir() if self.root.is_dir() else ():
            if directory.is_dir():
                try:
                    directory.rmdir()
                except OSError:
                    pass
        return removed


def _mobile_orders_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    return f"{parsed.scheme}://{parsed.netloc}{MOBILE_ORDERS_PATH}"


async def _find_mobile_card(page: Page, order_no: str):
    cards = page.locator(MOBILE_ORDER_CARD_SELECTOR)
    target = cards.filter(has_text=order_no)
    for _ in range(12):
        if await target.count():
            return target.first
        load_more = page.get_by_role("button", name=_LOAD_MORE_TEXT)
        if not await load_more.count() or not await load_more.first.is_visible():
            break
        previous_count = await cards.count()
        await load_more.first.click()
        try:
            await page.wait_for_function(
                "([selector, count]) => document.querySelectorAll(selector).length > count",
                arg=[MOBILE_ORDER_CARD_SELECTOR, previous_count],
                timeout=5_000,
            )
        except Exception:
            break
    return None


async def capture_missing_order_proofs(
    page: Page,
    base_url: str,
    orders: list[Order],
    store: OrderProofStore,
    *,
    today: date | None = None,
) -> dict[str, int]:
    """Capture mobile cards for current/future orders not already cached."""
    today = today or date.today()
    candidates = [
        order for order in orders
        if order.pay_status == PAID_STATUS
        and order.order_status == NORMAL_STATUS
        and (_date_value(order.use_date) or date.min) >= today
    ]
    missing = [
        order for order in candidates
        if store.cached_path(order.user, order.order_no) is None
    ]
    if not missing:
        return {"captured": 0, "cached": len(candidates), "failed": 0}

    _assert_cjk_font_available()
    old_viewport = page.viewport_size
    captured = 0
    failed = 0
    try:
        await page.set_viewport_size(PROOF_VIEWPORT)
        await page.goto(_mobile_orders_url(base_url), wait_until="domcontentloaded")
        await page.evaluate("async () => { await document.fonts.ready; }")
        await page.locator(MOBILE_ORDER_CARD_SELECTOR).first.wait_for(
            state="visible", timeout=10_000,
        )
        for order in missing:
            try:
                card = await _find_mobile_card(page, order.order_no)
                if card is None:
                    raise RuntimeError("matching mobile order card not found")
                target = store.target_path(order.user, order.order_no)
                target.parent.mkdir(parents=True, exist_ok=True)
                temp = target.with_name(f".{target.stem}.tmp.png")
                await card.screenshot(path=str(temp), animations="disabled")
                try:
                    os.chmod(temp, 0o600)
                except OSError:
                    pass
                temp.replace(target)
                store.record(order, target)
                captured += 1
            except Exception as exc:
                failed += 1
                log.warning(
                    "order proofs: failed for user=%s order=%s: %s",
                    order.user, order.order_no, exc,
                )
    finally:
        if old_viewport is not None:
            try:
                await page.set_viewport_size(old_viewport)
            except Exception:
                pass
    return {
        "captured": captured,
        "cached": len(candidates) - len(missing),
        "failed": failed,
    }
