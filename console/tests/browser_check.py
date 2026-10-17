"""Local Chrome interaction checks; no user profile or external network."""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(args):
    results = []
    errors = []
    chat_requests = []
    outside = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
        )
        context = await browser.new_context(viewport={"width": 1536, "height": 1050})

        async def route(request):
            if request.request.url.startswith(
                args.url
            ) or request.request.url.startswith("data:"):
                await request.continue_()
            else:
                outside.append(request.request.url)
                await request.abort()

        await context.route("**/*", route)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.on(
            "request",
            lambda r: (
                chat_requests.append(r.post_data_json)
                if r.url == args.url + "/api/chat" and r.method == "POST"
                else None
            ),
        )
        await page.goto(args.url)
        await page.locator("#mode").wait_for()
        await page.select_option("#scenario", "/mock reconnect")
        await page.locator("#send-chat").dblclick()
        await page.locator(".eventrow").first.wait_for()
        await page.locator("#disconnect").click()
        await page.wait_for_timeout(200)
        await page.locator("#reconnect").click()
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        first_time = await page.locator(".eventcard .eventtime").first.inner_text()
        assert first_time.endswith("초") and len(first_time.rsplit(".", 1)[-1]) == 2
        await page.locator("#retry-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        assert (
            await page.locator(".eventcard .eventtime").first.inner_text() == first_time
        )
        assert len(chat_requests) == 2 and chat_requests[0] == chat_requests[1]
        results.append(
            "same-body actual retry; original monotonic elapsed timestamp retained"
        )
        ids = await page.locator(".eventnum").all_text_contents()
        assert len([x for x in ids if x != "#—"]) == len({x for x in ids if x != "#—"})
        results.append(
            "double-click one request; disconnect/reconnect deduplicated and stored completion"
        )
        await page.locator(".eventrow").filter(has_text="answer.delta").first.click()
        assert "[원문 숨김]" in await page.locator("#event-json").inner_text()
        selected = await page.locator("#event-meta").inner_text()
        await page.locator("#autoscroll").uncheck()
        await page.fill("#event-filter", "finished")
        assert await page.locator("#event-meta").inner_text() == selected
        await page.fill("#event-filter", "")
        results.append(
            "raw hidden; filtered selection retained; auto-scroll can be disabled"
        )
        await page.screenshot(
            path=str(args.output.with_suffix(".chat.png")), full_page=True
        )
        await page.locator("#new-chat").click()
        await page.select_option("#scenario", "/mock slow")
        await page.locator("#send-chat").click()
        await page.locator("#cancel-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("CANCELLED")
        results.append("explicit cancellation reconciled against stored status")
        await page.locator("#new-chat").click()
        await page.select_option("#scenario", "/mock failed")
        await page.locator("#send-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("FAILED")
        results.append("failed stream stays failed; no EOF success")
        await page.locator('[data-page="profile"]').click()
        await page.fill("#recipient", str(args.recipient))
        await page.locator("#send-profile").click()
        await expect(page.locator("#profile-status")).to_have_text("DELIVERED")
        assert await page.locator("#profile-http").inner_text() == "202"
        results.append("profile202/result/callback tracked separately")
        if args.demo:
            await page.fill("#version", "2")
            await page.fill("#preference", "/fail SYNTHETIC")
            await page.locator("#send-profile").click()
            await expect(page.locator("#profile-status")).to_have_text("FAILED")
            results.append("profile failure displayed")
        await page.locator('[data-page="dashboard"]').click()
        await expect(page.locator(".servicecard")).to_have_count(4)
        assert (
            "미수집" in await page.locator("#cost").inner_text() if args.demo else True
        )
        await page.screenshot(
            path=str(args.output.with_suffix(".dashboard.png")), full_page=True
        )
        results.append("server observation source and unmeasured metrics displayed")
        await page.set_viewport_size({"width": 760, "height": 1080})
        await page.screenshot(
            path=str(args.output.with_suffix(".mobile.png")), full_page=True
        )
        assert not errors and not outside
        await browser.close()
    result = {
        "passed": True,
        "tests": results,
        "browserErrors": errors,
        "externalRequests": len(outside),
        "browser": "Existing MBP Chrome isolated context",
        "screenshots": [
            str(args.output.with_suffix(".chat.png")),
            str(args.output.with_suffix(".dashboard.png")),
            str(args.output.with_suffix(".mobile.png")),
        ],
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--demo", action="store_true")
    parser.add_argument(
        "--recipient",
        type=int,
        default=42,
        help="Use a fresh synthetic recipient for a repeated run",
    )
    asyncio.run(main(parser.parse_args()))
