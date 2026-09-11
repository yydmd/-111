"""Read-only smoke check of the local management page. Never books a seat."""
import json
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parents[1]


def main():
    with sync_playwright() as runtime:
        browser = runtime.chromium.launch(headless=True)
        try:
            context = browser.new_context(viewport={"width": 1400, "height": 1000}, service_workers="block")
            def readonly(route):
                parsed = urlparse(route.request.url)
                if (parsed.scheme, parsed.hostname, parsed.port) == ("http", "127.0.0.1", 8787) and route.request.method == "GET":
                    route.continue_()
                else:
                    route.abort()
            context.route("**/*", readonly)
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            page.on("console", lambda message: errors.append(message.text) if message.type == "error" else None)
            response = page.goto("http://127.0.0.1:8787/", wait_until="networkidle")
            assert response.status == 200
            page.wait_for_selector("#plans .plan-card")
            report = page.evaluate("""async () => {
                const plans = await api('/api/plans');
                const before = document.querySelectorAll('#plans .plan-card').length;
                await refreshPlanStatus();
                return {title:document.title, plans:plans.length, cards:before,
                    scheduleFields:plans.every(p=>'schedule_status' in p && 'account_enabled' in p),
                    disabledAccountLabel:nextRunDisplay({enabled:true,account_enabled:false})==='账号已停用',
                    rendered:plans.every(p=>{
                        const card=document.querySelector(`[data-plan-id="${p.id}"]`);
                        return card && card.querySelectorAll('.plan-fact b')[3].textContent===nextRunDisplay(p)
                            && card.querySelector('.schedule-note').textContent===(p.schedule_status?.message||'');
                    })};
            }""")
            output = ROOT / "data" / "qa" / "bugfix-20260911-management.png"
            output.parent.mkdir(parents=True, exist_ok=True)
            # Local screenshot masks account identifiers and input fields.
            page.screenshot(path=str(output), full_page=True,
                            mask=[page.locator("#accounts"), page.locator("input"), page.locator("select")])
            print(json.dumps({**report, "browser_errors": len(errors), "screenshot": str(output)}, ensure_ascii=False))
            assert report["cards"] == report["plans"] > 0 and report["rendered"], report
            assert report["scheduleFields"] and report["disabledAccountLabel"], report
            assert not errors, errors
        finally:
            browser.close()


if __name__ == "__main__":
    main()
