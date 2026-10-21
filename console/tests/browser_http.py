"""HTTP history UI checks using free local services and synthetic failures."""

import argparse
import asyncio
import json
import getpass
import re
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def main(args):
    password = getattr(args, 'password', None) or getpass.getpass('Console administrator password: ')
    errors = []
    outside = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
        )
        context = await browser.new_context(viewport={"width": 1536, "height": 1100})

        async def local(route):
            if route.request.url.startswith(args.url + "/"):
                await route.continue_()
            else:
                outside.append(route.request.url)
                await route.abort()

        await context.route("**/*", local)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(args.url)
        await page.fill('#admin-password', password)
        await page.click('#login-submit')
        await expect(page.locator('#mode')).not_to_have_text('—')
        await page.locator('[data-page="chat"]').click()
        await page.locator('.chat-options:not(.quick-check) > summary').click()
        await page.locator("#send-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        await page.locator("#retry-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        await page.locator("#new-chat").click()
        await page.select_option("#scenario", "/mock slow")
        await page.locator("#send-chat").click()
        await page.locator("#cancel-chat").click()
        await expect(page.locator("#chat-status")).to_have_text("CANCELLED")
        await page.locator('[data-page="profile"]').click()
        await page.fill("#recipient", str(args.recipient))
        await page.fill("#preference", "PRIVATE_PREFERENCE Bearer fake-secret")
        await page.locator("#send-profile").click()
        await expect(page.locator("#profile-status")).to_have_text("DELIVERED")
        for _ in range(3):
            await page.locator("#refresh-profile").click()
            await page.wait_for_timeout(100)
        await page.locator('[data-page="network"]').click()
        await expect(page.locator(".httprow")).not_to_have_count(0)
        before = int(await page.locator("#http-count").inner_text())
        assert await page.locator(".httppoll").count() >= 2
        await page.locator(".httppoll").first.locator("summary").first.click()
        assert int(await page.locator("#http-count").inner_text()) == before
        await page.locator("#http-polls").check()
        assert await page.locator(".httprow").count() == before
        text = await page.locator("#http-list").text_content()
        assert "PRIVATE_" not in text and "fake-secret" not in text
        assert "POST" in text and "PUT" in text and "/events" in text
        assert "시작 +" in text and "소요 " in text
        if args.live:
            assert "AI 200 → 201 → 202" in text
            retry_traces = await page.evaluate("""() => Array.from(document.querySelectorAll('.httprow'))
                .filter(row => row.querySelector('.httpmethod')?.textContent === 'POST'
                    && row.querySelector('.httppath')?.textContent === '/api/chat')
                .map(row => JSON.parse(row.querySelector('pre').textContent).upstream.exchanges)""")
            assert len(retry_traces[1]) == 1 and retry_traces[1][0]["method"] == "GET"
            assert "upstream 미호출" in text
        # Controlled relay fixtures: no external/paid requests; actual transport logic is covered by Python MockTransport tests.
        mode = 429

        async def failure(route):
            if mode == "validation":
                await route.fulfill(
                    status=422,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "detail": [
                                {
                                    "type": "string_type",
                                    "loc": ["body", "giftPreference"],
                                    "msg": "Value error, SYNTHETIC_PRIVATE_CANARY",
                                    "input": "SYNTHETIC_PRIVATE_CANARY",
                                    "ctx": {"error": "SYNTHETIC_PRIVATE_CANARY"},
                                }
                            ]
                        }
                    ),
                )
                return
            if mode == "connection":
                await route.abort("connectionfailed")
                return
            await page.wait_for_timeout(150)
            body = route.request.post_data_json
            exchange = {
                "method": "PUT",
                "path": "/v1/chat/test/conversations/"
                + body["conversation_id"]
                + "/turns/"
                + body["turn_id"],
                "status": mode,
                "headers": {
                    "retry-after": "2",
                    "authorization": "Bearer PRIVATE_AUTH",
                    "set-cookie": "PRIVATE_COOKIE",
                },
                "requestBody": {"message": "PRIVATE_REQUEST"},
                "responseBody": {
                    "error": {
                        "code": "SYNTHETIC_HTTP_FAILURE",
                        "message": "PRIVATE_RESPONSE",
                    }
                },
                "durationMs": 150,
            }
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "httpStatus": mode,
                        "body": {
                            "error": {
                                "code": "SYNTHETIC_HTTP_FAILURE",
                                "message": "PRIVATE_RESPONSE",
                            }
                        },
                        "transport": {"mode": "attempted", "exchanges": [exchange]},
                    }
                ),
            )

        await page.route("**/api/chat", failure)
        for status in [429, 503, "connection", "validation"]:
            mode = status
            await page.locator('[data-page="chat"]').click()
            await page.locator("#new-chat").click()
            await page.locator("#send-chat").click()
            await expect(page.locator("#chat-status")).to_have_text(re.compile(r"^접수 확인 실패"))
        await page.locator('.chat-debug > summary').click()
        await page.locator("#reveal").check()
        await page.locator('[data-page="network"]').click()
        await page.locator("#http-polls").uncheck()
        text = await page.locator("#http-list").text_content()
        assert "중계 200 · AI 429" in text and "중계 200 · AI 503" in text
        assert "중계 상태 미수신 · upstream 상태 미수신" in text
        assert "중계 422" in text and "SYNTHETIC_PRIVATE_CANARY" not in text
        assert "Retry-After 2" in text
        assert "PRIVATE_" not in text and "fake-secret" not in text
        await page.locator("#http-errors").check()
        # Two uncertain sends verify storage; their missing turns remain folded.
        assert await page.locator(".httprow").count() == 4
        assert await page.locator(".httppoll").count() == 2
        await expect(page.locator("#toast")).to_be_hidden()
        await page.screenshot(path=str(args.output.with_suffix(".png")), full_page=True)
        await page.locator("#http-errors").uncheck()
        assert int(await page.locator("#http-count").inner_text()) == before + 6
        assert not errors and not outside
        await browser.close()
    result = {
        "passed": True,
        "checks": [
            "real acceptance and same UUID retry retained",
            "state/cancellation/SSE HTTP recorded",
            "profile acceptance and individual poll records retained behind summary",
            "relay200 versus synthetic upstream429/503",
            "response-before-connection-failure has no status200",
            "Retry-After allowlist and permanent masking",
            "422 input/ctx/message canary absent from retained rendered HTTP records",
            "elapsed origin distinct from HTTP duration",
        ],
        "browserErrors": errors,
        "externalRequests": len(outside),
        "paidCalls": 0,
        "sourceMode": "free live service plus browser failure fixtures"
        if args.live
        else "built-in demo plus browser failure fixtures",
        "recordCount": before + 6,
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--live", action="store_true")
    parser.add_argument(
        "--recipient",
        type=int,
        default=90076,
        help="Fresh synthetic recipient for repeated checks",
    )
    asyncio.run(main(parser.parse_args()))
