"""Run against `uvicorn tests.browser_server:app --port 8081` with browser extra installed."""

import json
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

from tests.conftest import URLS

root = Path("/tmp/pricewatch-browser-smoke")
root.mkdir(exist_ok=True)
errors = []
with sync_playwright() as playwright:
    browser = playwright.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1440, "height": 1050})
    page.on("pageerror", lambda error: errors.append(str(error)))
    # Actual unmodified app instance.
    page.goto("http://127.0.0.1:8080/")
    expect(page.get_by_role("heading", name="Your watchlist")).to_be_visible()
    page.screenshot(path=str(root / "dashboard-empty.png"), full_page=True)
    # Isolated instance uses mocked retailers but all real routes, templates and persistence.
    page.goto("http://127.0.0.1:8081/products/new")
    page.locator('input[name="urls"]').fill(URLS["worten"])
    page.get_by_role("button", name="Add another URL").click()
    page.locator('input[name="urls"]').nth(1).fill(URLS["worten"] + "?utm_source=duplicate")
    page.locator('input[name="target_price"]').fill("900")
    page.locator('input[name="insane_deal_price"]').fill("800")
    page.get_by_text("Also search known stores").click()
    for store in page.locator('input[name="retailers"]').all():
        store.check()
    page.get_by_role("button", name="Review listings").click()
    expect(page.get_by_role("button", name="Confirm and monitor")).to_be_visible(timeout=20000)
    expect(page.locator('input[name="selected"]:checked')).to_have_count(5)
    page.screenshot(path=str(root / "discovery.png"), full_page=True)
    page.get_by_role("button", name="Confirm and monitor").click()
    expect(page.get_by_role("heading", name="TCL 85C7K")).to_be_visible()
    expect(page.locator("table tbody tr")).to_have_count(5)
    page.wait_for_function(
        'window.Chart && Chart.getChart(document.querySelector("#price-chart"))?.data.datasets.length === 5'
    )
    page.screenshot(path=str(root / "product.png"), full_page=True)
    page.locator("#range-select").select_option("0")
    page.locator("#series-select").select_option("0")
    page.wait_for_function(
        'Chart.getChart(document.querySelector("#price-chart"))?.data.datasets.length === 1'
    )
    (root / "price.txt").write_text("789")
    page.get_by_role("button", name="Check now").click()
    expect(page.locator(".stats")).to_contain_text("€789.00", timeout=20000)
    expect(page.locator(".page-header")).to_contain_text("INSANE DEAL")
    page.goto("http://127.0.0.1:8081/")
    expect(page.locator(".product-card")).to_have_count(1)
    page.screenshot(path=str(root / "dashboard.png"), full_page=True)
    page.goto("http://127.0.0.1:8081/alerts")
    expect(page.locator("tbody")).to_contain_text("insane deal")
    page.goto("http://127.0.0.1:8081/settings")
    expect(page.get_by_role("button", name="Save settings")).to_be_visible()
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto("http://127.0.0.1:8081/")
    expect(page.get_by_role("heading", name="Your watchlist")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= window.innerWidth")
    page.screenshot(path=str(root / "mobile.png"), full_page=True)
    browser.close()
assert not errors, errors
print(
    json.dumps(
        {
            "browser": "passed",
            "retailer_listings": 5,
            "duplicate_urls": "merged",
            "price_drop": "1199 -> 789",
            "charts": "5 series + range/filter",
            "console_errors": errors,
            "screenshots": str(root),
        }
    )
)
