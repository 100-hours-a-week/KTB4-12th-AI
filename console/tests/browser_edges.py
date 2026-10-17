"""Actual local profiler failure plus simulated collector outage/stale response."""

import argparse
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from playwright.async_api import async_playwright, expect


async def main(args):
    args.output.parent.mkdir(parents=True, exist_ok=True)
    info = json.loads(args.runtime.read_text())
    if info.get("paidCalls") != 0 or info.get("mode") != "local controlled fixture":
        raise ValueError("A controlled synthetic fixture runtime is required")
    for address in (info["url"], info["callbackFixtureURL"]):
        url = urlsplit(address)
        if (
            url.scheme != "http"
            or url.hostname not in {"127.0.0.1", "localhost", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Only fixed loopback fixture URLs are permitted")
    with httpx.Client(trust_env=False) as client:
        client.put(
            info["callbackFixtureURL"] + f"/fixture/mode/{args.recipient}/400"
        ).raise_for_status()
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
        )
        context = await browser.new_context(viewport={"width": 1536, "height": 1050})
        outside = []

        async def local(route):
            if route.request.url.startswith(info["url"] + "/"):
                await route.continue_()
            else:
                outside.append(route.request.url)
                await route.abort()

        await context.route("**/*", local)
        page = await context.new_page()
        await page.goto(info["url"])
        await page.locator('[data-page="profile"]').click()
        await page.fill("#recipient", str(args.recipient))
        await page.fill("#preference", "합성 콜백 실패 시험")
        await page.locator("#send-profile").click()
        await expect(page.locator("#profile-status")).to_have_text("FAILED")
        assert "BE 저장 확인 아님" in await page.locator("#profile-source").inner_text()
        await page.screenshot(
            path=str(args.output.with_suffix(".failure.png")), full_page=True
        )
        await page.locator('[data-page="dashboard"]').click()
        await expect(page.locator(".servicecard")).to_have_count(4)
        # Same real API response source; omit unavailable values rather than use0.
        unavailable = {
            "collectedAt": "2001-01-01T00:00:00Z",
            "scope": "브라우저 시험의 수집 장애 fixture",
            "services": {
                s: {"status": "unknown", "source": "수집 실패"}
                for s in ["chat", "search", "profiling"]
            },
            "chatCounts": None,
            "profileCounts": None,
            "jobs": None,
            "budget": None,
            "collectionStatus": "partial",
        }
        await page.route(
            "**/api/snapshot",
            lambda r: r.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(unavailable),
            ),
        )
        await page.locator("#refresh-dashboard").click()
        await expect(page.locator("#freshness")).to_contain_text("STALE")
        assert await page.locator("#chat-counts").inner_text() == "미수집"
        assert "알 수 없음" in await page.locator("#job-rows").inner_text()
        await page.screenshot(
            path=str(args.output.with_suffix(".stale.png")), full_page=True
        )
        await browser.close()
        assert not outside
    result = {
        "passed": True,
        "actualLocalProfilerFailureObserved": True,
        "callback400IsNotBEStorageSuccess": True,
        "collectorOutageAndStaleUIInjectedByBrowserFixture": True,
        "unknownValuesNotZero": True,
        "externalRequests": len(outside),
    }
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recipient", type=int, default=90202)
    asyncio.run(main(parser.parse_args()))
