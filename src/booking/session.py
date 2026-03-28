"""Browser session alignment for DevTools MCP + Playwright.

Use this checklist when pairing with the assistant:

1. **Select the right tab** — MCP tools operate on the *selected* page. Use
   ``list_pages`` then ``select_page(pageId=..., bringToFront=true)`` so snapshots
   and network capture match the booking site.
2. **Milestone cadence** — After you finish a sub-step (e.g. login, pick date),
   ask for a snapshot; network lists reset on navigation unless preserved requests
   are enabled.
3. **Selectors** — Snapshot ``uid`` values are for MCP only. Record stable strings
   for Playwright (roles, labels, text, ``data-*``) in config ``selectors``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DevToolsAlignment:
    """Reference checklist; values are illustrative."""

    list_pages_tool: str = "list_pages"
    select_page_tool: str = "select_page"
    take_snapshot_tool: str = "take_snapshot"
    list_network_requests_tool: str = "list_network_requests"
    get_network_request_tool: str = "get_network_request"
    take_screenshot_tool: str = "take_screenshot"


def alignment_steps() -> tuple[str, ...]:
    """Return ordered steps for human + assistant discovery sessions."""
    return (
        "list_pages → select_page on the booking tab",
        "take_snapshot after each milestone you complete",
        "list_network_requests (xhr/fetch) after actions that hit the server",
        "get_network_request(reqid) for request/response bodies",
        "take_screenshot(uid=captcha element) to define crop for Playwright",
        "Copy stable locators into site_config.yaml selectors (never snapshot uids in code)",
    )
