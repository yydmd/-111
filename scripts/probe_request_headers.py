"""Offline diagnostic: what request signature does the program's auxiliary
traffic carry?

The booking itself is submitted by the official page (a real in-page XHR), but
two helper paths do NOT go through the page:

* ``app/browser_reserve.py`` ``lookup()`` reads the reservation list through
  ``context.request`` (Playwright's APIRequestContext).
* ``app/clock.py`` probes the platform clock with the Python ``requests``
  library.

Both hit the official site close to the decisive submit. This script shows how
their headers differ from a genuine in-page XHR, using only loopback traffic.

It NEVER contacts the official service and never reads the app database,
cookies or browser profiles. No browser window is shown (headless), and the
only server involved is a local one bound to 127.0.0.1.
"""
from __future__ import annotations

import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8791
CAPTURED: dict[str, dict[str, str]] = {}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 (http.server API)
        CAPTURED[self.path] = dict(self.headers.items())
        body = b"ok"
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # keep the console readable
        pass


def _show(label: str, headers: dict[str, str]) -> dict[str, str]:
    print(f"--- {label} ---")
    lowered = {key.lower(): value for key, value in headers.items()}
    for key in sorted(lowered):
        print(f"  {key}: {lowered[key]}")
    return lowered


def main() -> int:
    server = HTTPServer(("127.0.0.1", PORT), _Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context()
            page = context.new_page()
            page.goto(f"http://127.0.0.1:{PORT}/page")
            page.evaluate("() => fetch('/from_page').then(r => r.text())")
            page.wait_for_timeout(300)
            context.request.get(f"http://127.0.0.1:{PORT}/from_api_request")
            browser.close()
    finally:
        server.shutdown()

    page_headers = _show("in-page XHR (/from_page)", CAPTURED.get("/from_page", {}))
    api_headers = _show("context.request (/from_api_request)", CAPTURED.get("/from_api_request", {}))

    only_page = sorted(set(page_headers) - set(api_headers))
    only_api = sorted(set(api_headers) - set(page_headers))
    print("=== headers a page XHR sends but context.request does NOT ===")
    print(only_page)
    print("=== headers only context.request sends ===")
    print(only_api)
    print("=== same header, different value ===")
    for key in sorted(set(page_headers) & set(api_headers)):
        if page_headers[key] != api_headers[key]:
            print(f"  {key}: page={page_headers[key]!r} api={api_headers[key]!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
