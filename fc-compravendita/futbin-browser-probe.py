from pathlib import Path
from playwright.sync_api import sync_playwright

url="https://www.futbin.com/27/players?search=Bradley%20Barcola"
out=Path("fc-compravendita/history/probe")
out.mkdir(parents=True, exist_ok=True)

with sync_playwright() as p:
    browser=p.chromium.launch(headless=True)
    context=browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/139.0.0.0 Safari/537.36",
        locale="en-US",
        viewport={"width":1440,"height":1000},
    )
    page=context.new_page()
    resp=page.goto(url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(5000)
    html=page.content()
    (out/"page.html").write_text(html, encoding="utf-8")
    (out/"result.txt").write_text(
        f"status={resp.status if resp else None}\ntitle={page.title()}\nurl={page.url}\nlen={len(html)}\n",
        encoding="utf-8",
    )
    browser.close()
