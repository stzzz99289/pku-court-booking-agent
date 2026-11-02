"""Fetch a user's recent paid bookings from /venue/orders.

The orders page renders a single iView table with these columns:
  序号, 订单类型/订单号, 预约场馆, 使用日期, 场地及使用时间,
  支付状态, 订单状态, 金额/支付方式, 下单时间, 操作

We use the DOM for payment/status labels and pagination, supplementing blank
display cells from the page's own order-list response by exact order number.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import asdict, dataclass
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse

# Transient navigation errors that warrant one retry (mirror of runner.py's set).
_GOTO_TRANSIENT_PATTERNS = (
    "Timeout 30000ms exceeded",
    "net::ERR_ABORTED",
    "net::ERR_NETWORK_CHANGED",
    "net::ERR_CONNECTION_RESET",
    "net::ERR_CONNECTION_CLOSED",
    "net::ERR_CONNECTION_REFUSED",
    "net::ERR_NAME_NOT_RESOLVED",
)


async def _goto_with_retry(page, url: str, *, wait_until: str = "domcontentloaded") -> None:
    """page.goto wrapper that retries once on transient network errors."""
    try:
        await page.goto(url, wait_until=wait_until)
        return
    except Exception as e:
        if not any(p in str(e) for p in _GOTO_TRANSIENT_PATTERNS):
            raise
        logging.getLogger(__name__).warning(
            "goto %s failed (%s); retrying once after 500ms.", url, e)
    await asyncio.sleep(0.5)
    await page.goto(url, wait_until=wait_until)

from playwright.async_api import Page

from .browser import dispose_context, launch_persistent_context
from .captcha import ManualCaptchaSolver
from .config import AppConfig, UserConfig
from .login import ensure_logged_in, session_token_expired
from .session_verification import mark_session_verified
from .site_constants import ORDERS_MINE_PATH

log = logging.getLogger(__name__)

PAID_STATUS = "已支付"
_ORDER_ROWS_TIMEOUT_S = 10.0
_ORDER_ROWS_POLL_S = 0.25
_ORDER_PAGE_ATTEMPTS = 2


@dataclass
class Order:
    """One row from the orders table, normalized for downstream display/serialization."""
    user: str
    order_no: str
    order_type: str
    venue: str
    use_date: str
    court_and_time: str
    pay_status: str
    order_status: str
    amount: str
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _orders_url(cfg: AppConfig) -> str:
    parsed = urlparse(cfg.base_url)
    return f"{parsed.scheme}://{parsed.netloc}/venue/orders"


def _split_type_and_no(cell: str) -> tuple[str, str]:
    # Cell format: "[在线预约] D260426000385"
    s = cell.strip()
    if s.startswith("[") and "]" in s:
        end = s.index("]")
        return s[1:end].strip(), s[end + 1:].strip()
    return "", s


def _parse_row(user: str, cells: list[str]) -> Order | None:
    if len(cells) < 10:
        return None
    order_type, order_no = _split_type_and_no(cells[1])
    return Order(
        user=user,
        order_no=order_no,
        order_type=order_type,
        venue=cells[2],
        use_date=cells[3],
        court_and_time=cells[4],
        pay_status=cells[5],
        order_status=cells[6],
        amount=cells[7],
        created_at=cells[8],
    )


def _order_display_details(payload: Any) -> dict[str, dict[str, str]]:
    """Retain only display fields, never the response's phone/user/session data."""
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("content") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    details: dict[str, dict[str, str]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        def text(key: str) -> str:
            value = row.get(key)
            return value.strip() if isinstance(value, str) else ""

        order_no = text("tradeNo")
        if order_no:
            details[order_no] = {
                # Preserve the desktop's historical venue-filter value.
                "venue": text("venueName") + text("siteName"),
                "court_and_time": text("reservationDateDetail"),
            }
    return details


def _apply_order_details(orders: list[Order], details: dict[str, dict[str, str]]) -> None:
    for order in orders:
        for field, value in details.get(order.order_no, {}).items():
            if not getattr(order, field).strip() and value:
                setattr(order, field, value)
    incomplete = [o for o in orders if o.order_status == "正常"
                  and (not o.venue.strip() or not o.court_and_time.strip())]
    if incomplete:
        raise RuntimeError(
            f"Order query returned {len(incomplete)} active order(s) without venue "
            "or court/time details; refusing to replace the cache with incomplete data."
        )


class _OrderDetailsCapture:
    """Passively read responses already requested by this user's orders page."""

    def __init__(self, base_url: str) -> None:
        url = urlparse(base_url)
        self.origin = (url.scheme, url.netloc)
        self.details: dict[str, dict[str, str]] = {}
        self.tasks: list[asyncio.Task] = []

    def on_response(self, response) -> None:
        url = urlparse(response.url)
        if ((url.scheme, url.netloc) == self.origin and url.path == ORDERS_MINE_PATH
                and response.request.method == "GET" and response.status == 200):
            self.tasks.append(asyncio.create_task(self._read(response)))

    async def _read(self, response) -> None:
        try:
            self.details.update(_order_display_details(await response.json()))
        except Exception:
            # Do not log the raw response, URL query, or authentication data.
            log.warning("Could not read supplemental order display details.")

    async def finish(self) -> None:
        if self.tasks:
            await asyncio.wait_for(asyncio.gather(*self.tasks), timeout=5.0)


async def _scrape_orders_table(page: Page, user: str) -> list[Order]:
    """Atomically parse all order rows. Rows are sorted newest-first by the site."""
    rows: list[list[str]] = await page.locator("table tbody tr").evaluate_all(
        """rows => {
            return rows.map(tr =>
                Array.from(tr.querySelectorAll('td')).map(td => td.innerText.trim())
            );
        }"""
    )
    out: list[Order] = []
    for cells in rows:
        order = _parse_row(user, cells)
        if order is not None:
            out.append(order)
    return out


async def _wait_for_order_rows(
    page: Page,
    user: str,
    timeout_s: float = _ORDER_ROWS_TIMEOUT_S,
) -> list[Order]:
    """Poll through Vue's transient empty-table render until real rows appear."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while True:
        rows = await _scrape_orders_table(page, user)
        if rows:
            return rows
        if loop.time() >= deadline:
            return []
        await asyncio.sleep(_ORDER_ROWS_POLL_S)


async def _dismiss_visible_confirm(page: Page) -> None:
    """Dismiss an authentication warning modal if its confirm button is visible."""
    try:
        confirm = page.get_by_role("button", name="确定").first
        if await confirm.count() and await confirm.is_visible():
            await confirm.click()
    except Exception:
        pass


async def _orders_session_rejected(page: Page) -> bool:
    """Return True when the protected orders page rejects the saved session."""
    modal = page.get_by_text("请登录后访问", exact=True)
    try:
        await modal.wait_for(state="visible", timeout=1_500)
        return True
    except Exception:
        return await session_token_expired(page)


async def fetch_user_orders(
    cfg: AppConfig,
    user: UserConfig,
    limit: int,
    *,
    after_fetch: Callable[[Page, list[Order]], Awaitable[None]] | None = None,
) -> list[Order]:
    """Login as `user`, navigate to /venue/orders, return up to `limit` paid orders (newest first)."""
    solver = ManualCaptchaSolver() if cfg.debug else _default_login_solver()
    context, _ = await launch_persistent_context(cfg)
    page = context.pages[0] if context.pages else await context.new_page()
    details = _OrderDetailsCapture(cfg.base_url)
    try:
        await _goto_with_retry(page, cfg.base_url)
        await ensure_logged_in(page, cfg, solver)
        page.on("response", details.on_response)
        url = _orders_url(cfg)
        initial_rows: list[Order] = []
        for attempt in range(1, _ORDER_PAGE_ATTEMPTS + 1):
            log.info(
                "Navigating to orders page: %s (attempt %d/%d)",
                url, attempt, _ORDER_PAGE_ATTEMPTS,
            )
            await _goto_with_retry(page, url)
            if await _orders_session_rejected(page):
                log.info(
                    "[%s] orders page rejected the saved session — forcing re-login.",
                    user.name,
                )
                await _dismiss_visible_confirm(page)
                await ensure_logged_in(page, cfg, solver, force=True)
                await _goto_with_retry(page, url)
                if await _orders_session_rejected(page):
                    raise RuntimeError(
                        f"Orders page still rejected the session for user {user.name} "
                        "after a fresh login."
                    )
            initial_rows = await _wait_for_order_rows(page, user.name)
            if initial_rows:
                break
            if attempt < _ORDER_PAGE_ATTEMPTS:
                log.warning(
                    "[%s] orders page stayed empty for %.0f s; navigating again before "
                    "accepting an empty result.",
                    user.name, _ORDER_ROWS_TIMEOUT_S,
                )

        if not initial_rows:
            log.warning(
                "Orders table remained empty after %d attempts for user %s.",
                _ORDER_PAGE_ATTEMPTS, user.name,
            )
            return []
        mark_session_verified(cfg.user_data_dir)
        orders = await _collect_paid_orders(
            page, user.name, limit, initial_rows=initial_rows,
        )
        page.remove_listener("response", details.on_response)
        await details.finish()
        _apply_order_details(orders, details.details)
        if after_fetch is not None:
            await after_fetch(page, orders)
        return orders
    finally:
        page.remove_listener("response", details.on_response)
        for task in details.tasks:
            if not task.done():
                task.cancel()
        if details.tasks:
            await asyncio.gather(*details.tasks, return_exceptions=True)
        await dispose_context(context)


async def _collect_paid_orders(
    page: Page,
    user: str,
    limit: int,
    *,
    initial_rows: list[Order] | None = None,
) -> list[Order]:
    """Scrape page after page until we have `limit` paid orders or run out.

    Dedup by `order_no` because the iView "next page" click occasionally lands
    back on the same data set (the row-text change check in `_go_to_next_page`
    can flip true during a loading flash even if the eventual rows are
    identical). If a page contributes zero new order_nos, treat it as the end
    of pagination instead of looping forever.
    """
    paid: list[Order] = []
    seen_paid: set[str] = set()
    seen_any: set[str] = set()
    page_num = 1
    while True:
        if page_num == 1 and initial_rows is not None:
            rows = initial_rows
        else:
            rows = await _wait_for_order_rows(page, user, timeout_s=5.0)
        statuses = sorted({o.pay_status for o in rows})
        new_any = sum(1 for o in rows if o.order_no and o.order_no not in seen_any)
        log.info("[%s] page %d: %d row(s) (%d new); pay_statuses=%s",
                 user, page_num, len(rows), new_any, statuses)
        for o in rows:
            if not o.order_no:
                continue
            seen_any.add(o.order_no)
            if o.pay_status == PAID_STATUS and o.order_no not in seen_paid:
                seen_paid.add(o.order_no)
                paid.append(o)
        if len(paid) >= limit:
            return paid[:limit]
        if new_any == 0:
            log.info("[%s] page %d returned no new order_nos; stopping pagination.",
                     user, page_num)
            return paid[:limit]
        if not await _go_to_next_page(page):
            return paid[:limit]
        page_num += 1


async def _go_to_next_page(page: Page) -> bool:
    """Click the iView pagination "next" arrow if it's enabled. Returns True if advanced."""
    next_btn = page.locator(".ivu-page-next").first
    if not await next_btn.count():
        return False
    cls = (await next_btn.get_attribute("class")) or ""
    if "ivu-page-disabled" in cls:
        return False
    # Snapshot the current first-row order_no to detect when the new page has rendered.
    prev_first = await page.evaluate(
        """() => {
            const tr = document.querySelector('table tbody tr');
            return tr ? tr.innerText : '';
        }"""
    )
    await next_btn.click()
    try:
        await page.wait_for_function(
            """prev => {
                const tr = document.querySelector('table tbody tr');
                return tr && tr.innerText !== prev;
            }""",
            arg=prev_first,
            timeout=5_000,
        )
    except Exception:
        log.warning("Next-page click did not refresh the table within 5 s; stopping pagination.")
        return False
    return True


def _default_login_solver():
    # Lazy import so a missing ddddocr install doesn't break debug mode.
    from .ocr import DdddocrSolver
    return DdddocrSolver()


def format_orders_table(orders: list[Order]) -> str:
    """Render orders as a fixed-width table for terminal display."""
    if not orders:
        return "(no paid orders)"
    headers = ["user", "order_no", "venue", "use_date", "court/time", "amount", "order_status", "created_at"]
    rows = [
        [o.user, o.order_no, o.venue, o.use_date, o.court_and_time, o.amount, o.order_status, o.created_at]
        for o in orders
    ]
    widths = [max(len(h), *(len(r[i]) for r in rows)) for i, h in enumerate(headers)]
    fmt = "  ".join(f"{{:<{w}}}" for w in widths)
    lines = [fmt.format(*headers), fmt.format(*("-" * w for w in widths))]
    lines.extend(fmt.format(*r) for r in rows)
    return "\n".join(lines)
