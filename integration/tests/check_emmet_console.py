"""Real Chrome GET-only view check on temporary synthetic integrated Console."""

import sys, json, asyncio
from pathlib import Path
from playwright.async_api import async_playwright, expect


async def main():
    base = sys.argv[1]
    output = Path(sys.argv[2])
    output.mkdir(exist_ok=True)
    errors = []
    blocked = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            args=["--disable-background-networking"],
        )
        context = await browser.new_context(viewport={"width": 1440, "height": 1000})

        async def guard(route):
            if not route.request.url.startswith(
                base + "/"
            ) or route.request.method not in ["GET", "HEAD"]:
                blocked.append(route.request.method)
                await route.abort()
                return
            await route.continue_()

        await context.route("**/*", guard)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(base + "/?view=dashboard")
        card = page.locator('[data-service="profiling"]')
        await expect(card.locator(".service-model")).to_have_text("openai/gpt-6-luna")
        await expect(card.locator(".op-badge")).to_have_text("준비됨")
        await expect(page.locator("#cost")).to_have_text("원장 미수집")
        await page.screenshot(path=str(output / "dashboard.png"), full_page=True)
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(100)
        assert not await page.evaluate(
            "document.documentElement.scrollWidth>innerWidth+2"
        )
        assert not errors and not blocked
        result = {
            "synthetic": True,
            "newLlmHealthRendered": True,
            "providerConnectivityNotClaimed": True,
            "missingCentralLedgerNotZero": True,
            "mobileNoOverflow": True,
            "pageErrors": errors,
            "blockedMutations": blocked,
            "paidCalls": 0,
        }
        (output / "result.json").write_text(json.dumps(result, indent=2))
        print(json.dumps(result))
        await browser.close()


asyncio.run(main())
