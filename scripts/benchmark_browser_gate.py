"""Offline benchmark for the browser request gate.

All traffic is fulfilled inside Playwright.  The script never contacts the
official service and never reads the application's database or browser data.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import browser_reserve as br


DAY = "2026-09-10"
VALUES = {
    "room_id": "10713",
    "seats": ["097"],
    "start_time": "08:30",
    "end_time": "09:30",
}
INTENT = {
    "roomId": "10713",
    "day": DAY,
    "seatNum": "097",
    "startTime": "08:30",
    "endTime": "09:30",
}
HTML = """<!doctype html><meta charset=utf-8>
<button id=submit>submit</button>
<script>
document.querySelector('#submit').onclick = async () => {
  await fetch('/risk/token');
  await fetch('/data/apps/seat/submit', {
    method: 'POST', body: new URLSearchParams(%s)
  });
};
</script>""" % json.dumps(INTENT)


class UnitRoute:
    def __init__(self):
        self.request = type("Request", (), {
            "url": br.ORIGIN + br.SUBMIT_PATH,
            "post_data": urlencode(INTENT),
        })()

    def fallback(self):
        return None

    def abort(self):
        raise AssertionError("valid benchmark request was blocked")


def percentile(values: list[float], percent: float) -> float:
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(len(ordered) * percent) - 1))
    return round(ordered[index], 3)


def unit_benchmark(iterations: int) -> dict:
    durations = []
    for _ in range(iterations):
        gate = br.RequestGate(
            VALUES, DAY, False, lambda *args: True,
            br.Control(), time.monotonic() + 30,
        )
        gate.armed_seat = "097"
        gate.mark_click("097")
        gate.route(UnitRoute())
        durations.append(gate.attempt_timings[0]["gate_handler_ms"])
    return {
        "iterations": iterations,
        "gate_handler_ms": {
            "median": round(statistics.median(durations), 3),
            "p95": percentile(durations, 0.95),
            "max": round(max(durations), 3),
        },
    }


def browser_benchmark(iterations: int) -> dict:
    from playwright.sync_api import sync_playwright

    results = {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(service_workers="block")
        request_counts = {"legacy": 0, "scoped": 0}
        callback_counts = {"legacy": 0, "scoped": 0}
        active_mode = ["legacy"]

        def transport(route):
            request_counts[active_mode[0]] += 1
            path = route.request.url.split("?", 1)[0]
            if path.endswith("/risk/token"):
                route.fulfill(json={"token": "offline-fixture"})
            elif path.endswith(br.SUBMIT_PATH):
                route.fulfill(json={"success": True})
            else:
                route.fulfill(body=HTML, content_type="text/html")

        context.route("**/*", transport)
        try:
            for mode in ("legacy", "scoped"):
                active_mode[0] = mode
                measurements = []
                callbacks = [0]
                for _ in range(iterations + 5):
                    if _ == 5:
                        request_counts[mode] = 0
                        callback_counts[mode] = 0
                    gate = br.RequestGate(
                        VALUES, DAY, False, lambda *args: True,
                        br.Control(), time.monotonic() + 30,
                    )
                    gate.armed_seat = "097"

                    class CountingGate:
                        """Count how many requests reach the Python callback.

                        Narrowing the routing scope is supposed to leave risk and
                        static traffic native, so the number of Python route
                        callbacks is the property this benchmark must evidence.
                        The wrapped handler is one stable object so
                        ``install_request_gate``/``remove_request_gate`` still
                        register and drop the exact same handler.
                        """

                        def __init__(self, wrapped):
                            self._wrapped = wrapped

                            def handler(route):
                                callbacks[0] += 1
                                return wrapped.route(route)

                            self.route = handler

                        def __getattr__(self, name):
                            return getattr(self._wrapped, name)

                    counting_gate = CountingGate(gate)
                    callbacks_before = callbacks[0]
                    if mode == "legacy":
                        context.route("**/*", counting_gate.route)
                    else:
                        br.install_request_gate(context, counting_gate)
                    context.on("response", gate.response)
                    page = context.new_page()
                    try:
                        page.goto(br.ORIGIN + "/front/third/apps/seat/select?id=10713")
                        gate.mark_click("097")
                        page.locator("#submit").click()
                        limit = time.monotonic() + 2
                        while (not gate.attempt_timings or
                               "request_to_response_ms" not in gate.attempt_timings[-1]):
                            if time.monotonic() >= limit:
                                raise TimeoutError("offline submit did not finish")
                            page.wait_for_timeout(5)
                        if _ >= 5:
                            measurements.append(dict(gate.attempt_timings[-1]))
                    finally:
                        callback_counts[mode] += callbacks[0] - callbacks_before
                        page.close()
                        context.remove_listener("response", gate.response)
                        if mode == "legacy":
                            context.unroute("**/*", counting_gate.route)
                        else:
                            br.remove_request_gate(context, counting_gate)
                results[mode] = {
                    "iterations": iterations,
                    "network_requests": request_counts[mode],
                    "python_route_callbacks": callback_counts[mode],
                    "click_to_request_ms_p95": percentile(
                        [item["click_to_request_ms"] for item in measurements], 0.95),
                    "gate_handler_ms_p95": percentile(
                        [item["gate_handler_ms"] for item in measurements], 0.95),
                    "request_to_response_ms_p95": percentile(
                        [item["request_to_response_ms"] for item in measurements], 0.95),
                }
        finally:
            browser.close()

    results["delta_scoped_minus_legacy"] = {
        "network_requests": results["scoped"]["network_requests"] - results["legacy"]["network_requests"],
        "python_route_callbacks": results["scoped"]["python_route_callbacks"] - results["legacy"]["python_route_callbacks"],
        "click_to_request_ms_p95": round(
            results["scoped"]["click_to_request_ms_p95"] - results["legacy"]["click_to_request_ms_p95"], 3),
        "gate_handler_ms_p95": round(
            results["scoped"]["gate_handler_ms_p95"] - results["legacy"]["gate_handler_ms_p95"], 3),
    }
    return results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser-iterations", type=int, default=50)
    parser.add_argument("--unit-iterations", type=int, default=1000)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.browser_iterations < 1 or args.unit_iterations < 1:
        parser.error("iteration counts must be positive")
    report = {
        "offline_only": True,
        "official_requests": 0,
        "unit": unit_benchmark(args.unit_iterations),
        "browser": browser_benchmark(args.browser_iterations),
    }
    report["accepted"] = bool(
        report["browser"]["delta_scoped_minus_legacy"]["network_requests"] == 0
        # Narrowing must leave at least the risk/static traffic native: the
        # scoped gate has to run strictly fewer Python route callbacks.
        and report["browser"]["delta_scoped_minus_legacy"]["python_route_callbacks"] < 0
        and report["browser"]["delta_scoped_minus_legacy"]["gate_handler_ms_p95"] <= 10
        and report["browser"]["delta_scoped_minus_legacy"]["click_to_request_ms_p95"] <= 10
        and report["unit"]["gate_handler_ms"]["p95"] <= 10
    )
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["accepted"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
