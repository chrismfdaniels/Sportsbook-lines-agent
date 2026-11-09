"""Shared headless-browser helper: load public pages and collect the JSON they fetch."""
from __future__ import annotations

import json


class BrowserSession:
    """One Chromium for many page loads (props visit several pages per game)."""

    def __enter__(self):
        from playwright.sync_api import sync_playwright
        self._pw = sync_playwright().start()
        try:
            self._browser = self._pw.chromium.launch(headless=True)
            self._ctx = self._browser.new_context(locale="en-US", timezone_id="America/New_York",
                                                  viewport={"width": 1400, "height": 1000})
        except Exception:
            self._pw.stop()
            raise
        return self

    def __exit__(self, *exc):
        try:
            self._browser.close()
        finally:
            self._pw.stop()

    def capture(self, url: str, url_contains: str, wait_ms: int = 8000, scrolls: int = 0) -> list:
        payloads: list = []

        def on_response(resp):
            if url_contains in resp.url and resp.status == 200 and "json" in resp.headers.get("content-type", ""):
                try:
                    payloads.append(json.loads(resp.body()))
                except Exception:
                    pass

        page = self._ctx.new_page()
        try:
            page.on("response", on_response)
            resp = page.goto(url, wait_until="domcontentloaded", timeout=45000)
            if resp is not None and resp.status >= 400:
                raise RuntimeError(f"{url} returned HTTP {resp.status}")
            page.wait_for_timeout(wait_ms)
            for _ in range(scrolls):
                page.mouse.wheel(0, 2500)
                page.wait_for_timeout(1500)
        finally:
            page.close()
        return payloads


def capture_json(url: str, url_contains: str, wait_ms: int = 8000, scrolls: int = 0) -> list:
    with BrowserSession() as s:
        return s.capture(url, url_contains, wait_ms=wait_ms, scrolls=scrolls)
