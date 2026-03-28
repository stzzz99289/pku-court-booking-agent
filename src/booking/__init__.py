"""Court booking automation package.

DevTools alignment (each session before discovery):
1. In Cursor, ensure Chrome DevTools MCP targets your booking tab: call ``list_pages``,
   then ``select_page`` with that tab's ``pageId``.
2. Take ``take_snapshot`` after each milestone you complete manually.
3. Derive stable Playwright selector strings and paste them into ``site_config.yaml`` under ``selectors``.
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
