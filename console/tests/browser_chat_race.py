"""Late state/cancellation/stream replies must not affect a newer free trial."""

import argparse
import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright, expect


async def case(browser, url, kind):
    context = await browser.new_context()
    outside = []
    errors = []
    requests = []
    if kind == "stream":
        # Model an old response that arrives even after its AbortSignal fired.
        await context.add_init_script("""(() => {
          const original = window.fetch; let first = true;
          window.fetch = (input, options = {}) => {
            if(first && String(input).includes('/events?')) {
              first = false; return original(input, {...options, signal: undefined});
            }
            return original(input, options);
          };
        })();""")

    async def local_only(route):
        if route.request.url.startswith(url + "/"):
            await route.continue_()
        else:
            outside.append(route.request.url)
            await route.abort()

    await context.route("**/*", local_only)
    page = await context.new_page()
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on(
        "request",
        lambda r: (
            requests.append(r.post_data_json)
            if r.url == url + "/api/chat" and r.method == "POST"
            else None
        ),
    )
    held = asyncio.Event()
    release = asyncio.Event()

    async def hold_old(route):
        path = route.request.url[len(url) :]
        matches = (
            (
                kind == "state"
                and route.request.method == "GET"
                and "/events" not in path
                and "/cancellation" not in path
            )
            or (kind == "cancel" and path.endswith("/cancellation"))
            or (kind == "stream" and "/events?" in path)
        )
        if matches and not held.is_set():
            old = await route.fetch() if kind == "cancel" else None
            held.set()
            await release.wait()
            if kind == "stream":
                await route.fulfill(
                    status=200,
                    content_type="text/event-stream",
                    body='id: 9999\nevent: answer.delta\ndata: {"delta":"STALE_STREAM_CANARY"}\n\n',
                )
            elif old:
                await route.fulfill(response=old)
            else:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {"data": {"status": "COMPLETED", "lastEventId": 6}}
                    ),
                )
        else:
            await route.continue_()

    await page.route("**/api/chat/**", hold_old)
    await page.goto(url)
    if kind != "state":
        await page.select_option("#scenario", "/mock slow")
    await page.locator("#send-chat").click()
    if kind == "cancel":
        await page.locator("#cancel-chat").click()
    await asyncio.wait_for(held.wait(), 6)
    await page.locator("#new-chat").click()
    await page.select_option("#scenario", "/mock slow")
    await page.locator("#send-chat").click()
    await expect(page.locator("#chat-status")).to_have_text("PROCESSING")
    release.set()
    await page.wait_for_timeout(250)
    current = await context.request.get(url + "/api/chat/" + requests[-1]["turn_id"])
    stored = (await current.json())["data"]["status"]
    assert stored == "PROCESSING"
    await expect(page.locator("#chat-status")).to_have_text("PROCESSING")
    assert not await page.locator("#cancel-chat").is_disabled()
    assert (
        not await page.locator(".eventcard")
        .filter(has_text="console.cancellation")
        .count()
    )
    assert "#9999" not in await page.locator("#event-list").inner_text()
    await page.locator("#reveal").check()
    assert "STALE_STREAM_CANARY" not in await page.locator("#event-list").inner_text()
    for request in requests:
        await context.request.put(
            url + "/api/chat/" + request["turn_id"] + "/cancellation",
            headers={"Origin": url},
            data={},
        )
    assert not outside and not errors
    await context.close()
    return {
        "kind": kind,
        "newStored": stored,
        "newUI": "PROCESSING",
        "cancelEnabled": True,
        "oldEventNotRecorded": True,
        "externalRequests": len(outside),
        "browserErrors": errors,
    }


async def main(args):
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
        )
        results = [
            await case(browser, args.url, kind)
            for kind in ["state", "cancel", "stream"]
        ]
        await browser.close()
    result = {"passed": True, "cases": results, "paidCalls": 0}
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
