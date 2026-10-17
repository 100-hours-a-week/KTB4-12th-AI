"""Free browser regressions: previews, request isolation, stale failure persistence."""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(args):
    errors = []
    outside = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
        )
        context = await browser.new_context(viewport={"width": 1536, "height": 1050})

        async def local_only(route):
            if route.request.url.startswith(args.url + "/"):
                await route.continue_()
            else:
                outside.append(route.request.url)
                await route.abort()

        await context.route("**/*", local_only)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(args.url)
        await page.locator("#send-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        await expect(page.locator(".eventcard.cards")).to_contain_text("상품 #101")
        assert (
            "[원문 숨김]" in await page.locator(".eventcard.answer").first.inner_text()
        )
        await page.locator("#reveal").check()
        assert (
            "[원문 숨김]"
            not in await page.locator(".eventcard.answer").first.inner_text()
        )
        await page.screenshot(path=str(args.output.with_suffix(".png")), full_page=True)
        await page.locator("#reveal").uncheck()
        assert (
            "[원문 숨김]" in await page.locator(".eventcard.answer").first.inner_text()
        )

        # Untrusted long data is plain text, with previews bounded and token masked.
        async def stream(route):
            delta = "<img src=x onerror=alert(1)> PRIVATE_CANARY Bearer fake-token " + (
                "long " * 150
            )
            body = (
                "id: 1\nevent: answer.delta\ndata: "
                + json.dumps({"delta": delta})
                + '\n\nid: 2\nevent: turn.finished\ndata: {"status":"COMPLETED","lastEventId":2,"blocks":[]}\n\n'
            )
            await route.fulfill(status=200, content_type="text/event-stream", body=body)

        await page.route("**/events?*", stream)
        await page.locator("#new-chat").click()
        await page.locator("#send-chat").click()
        await expect(page.locator(".eventcard.answer")).to_have_count(1)
        assert "PRIVATE_CANARY" not in await page.locator("#event-list").inner_text()
        await page.locator("#reveal").check()
        await expect(page.locator(".eventcard.answer")).to_contain_text("<img")
        assert await page.locator("#event-list img").count() == 0
        assert "fake-token" not in await page.locator("#event-list").inner_text()
        await page.locator(".preview-more").click()
        assert (
            len(await page.locator(".eventcard.answer .eventpreview").inner_text())
            > 260
        )
        await page.unroute("**/events?*", stream)
        await page.locator("#new-chat").click()
        await page.select_option("#scenario", "/mock slow")
        await page.locator("#autoscroll").uncheck()
        await page.locator("#send-chat").click()
        await expect(page.locator(".eventcard.answer").first).to_be_visible()
        await page.locator("#event-list").hover()
        await page.mouse.wheel(0, -10000)
        before = await page.locator(".eventcard").first.bounding_box()
        await page.wait_for_timeout(700)
        await expect(page.locator("#new-events")).to_be_visible()
        after = await page.locator(".eventcard").first.bounding_box()
        assert abs(before["y"] - after["y"]) < 2
        await page.locator("#new-events").click()
        await expect(page.locator("#new-events")).to_be_hidden()
        await page.locator("#cancel-chat").click()
        # A held result for version1 must not repopulate version2's rejected request.
        await page.locator('[data-page="profile"]').click()
        release = asyncio.Event()
        held = asyncio.Event()
        hold = False

        async def profile_post(route):
            body = route.request.post_data_json
            if body["sourceVersion"] == 2:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "httpStatus": 409,
                            "body": {"error": {"code": "TEST_REJECTION"}},
                        }
                    ),
                )
            else:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"httpStatus": 202}),
                )

        async def profile_get(route):
            if hold:
                held.set()
                await release.wait()
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "source": "synthetic old request",
                        "collectedAt": "2026-10-14T00:00:00Z",
                        "data": {"status": "RUNNING", "callbackAttempts": 9},
                    }
                ),
            )

        await page.route("**/api/profile", profile_post)
        await page.route("**/api/profile/90001/1", profile_get)
        await page.locator("#send-profile").click()
        await expect(page.locator("#profile-status")).to_have_text("RUNNING")
        hold = True
        await page.locator("#refresh-profile").click()
        await asyncio.wait_for(held.wait(), 3)
        await page.fill("#version", "2")
        await page.locator("#send-profile").click()
        await expect(page.locator("#profile-status")).to_have_text("접수 거절")
        release.set()
        await page.wait_for_timeout(1200)
        await expect(page.locator("#profile-status")).to_have_text("접수 거절")
        assert await page.locator("#profile-history").inner_text() == ""
        assert await page.locator("#profile-attempts").inner_text() == "미수집"
        assert await page.locator("#refresh-profile").is_disabled()
        # Collector failure survives the one-second display refresh.
        await page.locator('[data-page="dashboard"]').click()
        await expect(page.locator(".servicecard")).to_have_count(4)

        async def fail(route):
            await route.fulfill(
                status=503,
                content_type="application/json",
                body='{"detail":"synthetic outage"}',
            )

        await page.route("**/api/snapshot", fail)
        await page.locator("#refresh-dashboard").click()
        await expect(page.locator("#freshness")).to_contain_text("STALE")
        await page.wait_for_timeout(1400)
        await expect(page.locator("#freshness")).to_contain_text("STALE")
        await page.unroute("**/api/snapshot", fail)
        await page.locator("#refresh-dashboard").click()
        await expect(page.locator("#freshness")).not_to_contain_text("STALE")
        assert not errors and not outside
        await browser.close()
    result = {
        "passed": True,
        "checks": [
            "inline previews without selecting",
            "hide/reveal policy shared",
            "XSS inert and token masked",
            "long preview expansion",
            "reading position retained and new-event jump",
            "late old profile ignored after new rejection",
            "profile history and callback reset",
            "collector failure survives timer until success",
        ],
        "browserErrors": errors,
        "externalRequests": len(outside),
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
