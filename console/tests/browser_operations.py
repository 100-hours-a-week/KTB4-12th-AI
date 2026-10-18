"""Real Chromium UI checks; all mutations and snapshots are synthetic intercepts."""

import argparse
import asyncio
import copy
import json
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright, expect


def stamp():
    return datetime.now(timezone.utc).isoformat()


def snapshot():
    return {
        "collectedAt": stamp(),
        "scope": "합성 수용검증 · 실제 운영 수치 아님",
        "collectionStatus": "collected",
        "dataMode": "SYNTHETIC_BROWSER_TEST",
        "activeCounts": {"chat": 1, "profiling": 1},
        "chatCounts": {"PROCESSING": 1, "COMPLETED": 3},
        "profileCounts": {"RESULT_READY": 1},
        "sharedBudget": {
            "limitUsd": 5,
            "committedUsd": 3.6,
            "remainingUsd": 1.4,
            "validationAttempts": 4,
            "validationMaxAttempts": 100,
            "validationReservedUsd": 0.05,
            "validationMaxReservedUsd": 1,
            "halted": None,
        },
        "services": {
            "chat": {
                "status": "ready",
                "httpStatus": 200,
                "source": "/readyz",
                "probeMs": 28,
                "data": {
                    "ready": True,
                    "mode": "live",
                    "model": "openai/gpt-6-luna",
                    "agent": "gift-tool-loop-v1",
                    "reasoningLabel": "추론 low",
                    "prompt": "PRIVATE_PROMPT",
                    "unexpected": "PRIVATE_UNEXPECTED",
                    "providerResponse": "PRIVATE_PROVIDER",
                },
            },
            "search": {
                "status": "ready",
                "httpStatus": 200,
                "source": "/readyz",
                "probeMs": 11,
                "data": {
                    "status": "ready",
                    "products": 4231,
                    "snapshotId": "synthetic-snapshot",
                },
            },
            "profiling": {
                "status": "ready",
                "httpStatus": 200,
                "source": "/health",
                "probeMs": 19,
                "data": {
                    "status": "ok",
                    "catalog": {
                        "active": True,
                        "version": "synthetic-version",
                        "giftPreference": "PRIVATE_TASTE",
                    },
                    "store": {"connected": True, "migration": "0007"},
                    "supervisor": {
                        "running": 1,
                        "slots": 2,
                        "queued": 3,
                        "queue_max": 200,
                    },
                    "tagger": {
                        "enabled": True,
                        "required": True,
                        "model": "openai/gpt-6-luna",
                    },
                    "embedding": {"enabled": True},
                    "callbackUrl": "http://private.invalid?secret=PRIVATE_QUERY",
                },
            },
            "console": {
                "status": "ready",
                "source": "collector process · self report",
                "data": {
                    "mode": "SYNTHETIC_ACCEPTANCE",
                    "sourceRevision": "synthetic-v2",
                },
            },
        },
        "searchMetadata": {"model": "jinaai/jina-embeddings-v5-text-nano-retrieval"},
        "jobs": [
            {
                "id": "synthetic-chat-a",
                "service": "Chat",
                "status": "PROCESSING",
                "stage": "답변 스트리밍",
                "stageSource": "stored last event",
                "eventCursor": 2,
                "active": True,
                "elapsedSeconds": 1.2,
                "elapsedBasis": "합성 접수 경과",
                "created": stamp(),
            },
            {
                "id": "synthetic-profile-b",
                "service": "Profiler",
                "status": "RESULT_READY",
                "stage": "분석 완료 · 콜백 전달 대기",
                "callbackAttempts": 1,
                "active": False,
                "elapsedSeconds": None,
                "elapsedBasis": "종료 시간 미계측",
                "created": stamp(),
            },
        ],
    }


async def main(args):
    base = args.url.rstrip("/")
    assert urlsplit(base).hostname in ["127.0.0.1", "localhost"]
    args.output.mkdir(parents=True, exist_ok=True)
    state = {
        "snapshot": snapshot(),
        "outage": False,
        "snapshotGate": asyncio.Event(),
        "chatStatus": "PROCESSING",
        "postCodes": [429, 503, 202],
        "postBodies": [],
        "streamMode": "progress",
        "streamGate": asyncio.Event(),
        "getPaths": [],
        "mutations": 0,
    }
    errors = []
    outside = []
    checks = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            executable_path="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            headless=True,
            chromium_sandbox=True,
            args=["--disable-background-networking"],
        )
        context = await browser.new_context(
            viewport={"width": 1536, "height": 1100}, reduced_motion="reduce"
        )

        async def routing(route):
            req = route.request
            path = urlsplit(req.url).path
            if not req.url.startswith(base + "/"):
                outside.append(req.url)
                await route.abort()
                return
            if path == "/api/snapshot":
                await state["snapshotGate"].wait()
                if state["outage"]:
                    await route.abort()
                    return
                if state.get("invalidJson"):
                    await route.fulfill(
                        content_type="application/json",
                        body="PRIVATE_PROVIDER_INVALID_JSON",
                    )
                    return
                await route.fulfill(json=copy.deepcopy(state["snapshot"]))
                return
            if path == "/api/chat" and req.method == "POST":
                state["mutations"] += 1
                state["postBodies"].append(req.post_data_json)
                code = state["postCodes"].pop(0) if state["postCodes"] else 202
                await route.fulfill(
                    json={
                        "httpStatus": code,
                        "body": {
                            "data": {"status": "PROCESSING"},
                            "error": {
                                "code": "TOO_MANY_REQUESTS"
                                if code == 429
                                else "SERVICE_UNAVAILABLE",
                                "message": "PRIVATE_ERROR",
                                "providerResponse": "PRIVATE_BODY",
                                "callbackUrl": "http://private.invalid?token=PRIVATE_QUERY",
                            },
                        },
                        "transport": {
                            "mode": "attempted",
                            "exchanges": [
                                {
                                    "status": code,
                                    "headers": {"retry-after": "2"},
                                    "responseBody": {
                                        "error": {
                                            "code": "TOO_MANY_REQUESTS",
                                            "message": "PRIVATE_ERROR",
                                        },
                                        "prompt": "PRIVATE_PROMPT",
                                        "providerResponse": "PRIVATE_RESPONSE",
                                    },
                                }
                            ],
                        },
                    }
                )
                return
            if path.startswith("/api/chat/") and path.endswith("/events"):
                state["getPaths"].append(req.url)
                if state["streamMode"] == "hold":
                    await state["streamGate"].wait()
                body = (
                    'id: 1\nevent: answer.delta\ndata: {"delta":"합성 응답 조각"}\n\n'
                )
                if state["streamMode"] == "complete":
                    body += 'id: 2\nevent: cards\ndata: {"products":[{"productId":101,"reason":"합성 이유"},{"productId":102,"reason":"합성 이유"},{"productId":103,"reason":"합성 이유"}]}\n\nid: 3\nevent: turn.finished\ndata: {"status":"COMPLETED","lastEventId":3,"blocks":[]}\n\n'
                try:
                    await route.fulfill(
                        status=200, content_type="text/event-stream", body=body
                    )
                except PlaywrightError:
                    # The deliberate abort may close this synthetic route first.
                    checks.append("aborted synthetic stream closed")
                return
            if (
                path.startswith("/api/chat/")
                and path.endswith("/cancellation")
                and req.method == "PUT"
            ):
                state["mutations"] += 1
                await route.fulfill(
                    json={"httpStatus": 200, "body": {"data": {"status": "CANCELLED"}}}
                )
                return
            if path.startswith("/api/chat/") and req.method == "GET":
                # Intentionally old state after terminal stream; it must not regress.
                await route.fulfill(
                    json={"data": {"status": state["chatStatus"], "lastEventId": 1}}
                )
                return
            if req.method not in ["GET", "HEAD"]:
                raise AssertionError("Unintercepted mutation forbidden: " + path)
            await route.continue_()

        await context.route("**/*", routing)
        page = await context.new_page()
        page.on("pageerror", lambda e: errors.append(str(e)))
        await page.goto(base + "/?view=dashboard")
        await expect(page.locator("#freshness")).to_contain_text("수집 중")
        await expect(page.locator("#refresh-dashboard")).to_be_disabled()
        checks.append("loading does not fabricate values")
        state["snapshotGate"].set()
        await expect(page.locator(".servicecard")).to_have_count(4)
        await expect(page.locator("#cost")).to_contain_text("$3.60")
        await expect(page.locator("#budget-remaining")).to_have_text("$1.40")
        await expect(page.locator("#budget-actual")).to_have_text("미분리")
        clock = page.locator('[data-job="Chat/synthetic-chat-a"] .elapsed-live')
        first = await clock.inner_text()
        await page.wait_for_timeout(250)
        assert await clock.inner_text() != first
        checks.append("active elapsed updates in tenths")
        for detail in await page.locator(".service-inspect").all():
            await detail.locator("summary").click()
        assert "PRIVATE_" not in await page.locator("body").text_content()
        checks.append("health raw allowlist and canaries")
        await page.screenshot(
            path=str(args.output / "dashboard-active.png"), full_page=True
        )
        await page.select_option("#job-filter", "FAILED")
        await expect(page.locator(".job-empty")).to_contain_text("필터에 맞는")
        await page.select_option("#job-filter", "all")
        await page.fill("#job-query", "profile")
        await expect(page.locator("#job-rows tr")).to_have_count(1)
        await page.fill("#job-query", "")
        checks.append("job status and ID filters")
        before_outage = float((await clock.inner_text()).removesuffix("s"))
        state["outage"] = True
        await page.click("#refresh-dashboard")
        await expect(page.locator("#freshness")).to_contain_text("STALE")
        await expect(page.locator("#cost")).to_contain_text("$3.60")
        frozen = await clock.inner_text()
        assert float(frozen.removesuffix("s")) >= before_outage
        await page.wait_for_timeout(250)
        assert frozen == await clock.inner_text()
        checks.append("outage retains last values visibly stale and freezes elapsed")
        await page.screenshot(
            path=str(args.output / "dashboard-stale.png"), full_page=True
        )
        state["outage"] = False
        state["invalidJson"] = True
        await page.click("#refresh-dashboard")
        await expect(page.locator("#freshness")).to_contain_text("JSON")
        assert "PRIVATE_" not in await page.locator("body").text_content()
        state["invalidJson"] = False
        checks.append("malformed snapshot body never leaks through error text")
        current = snapshot()
        current["jobs"][0].update(
            status="COMPLETED", active=False, elapsedSeconds=4.7, stage="최종 상태 저장"
        )
        state["snapshot"] = current
        await page.click("#refresh-dashboard")
        await expect(clock).to_have_text("4.7s")
        await page.wait_for_timeout(250)
        await expect(clock).to_have_text("4.7s")
        old = snapshot()
        old["collectedAt"] = "2020-01-01T00:00:00Z"
        old["sharedBudget"]["committedUsd"] = 0.01
        state["snapshot"] = old
        await page.click("#refresh-dashboard")
        await expect(page.locator("#freshness")).to_contain_text("늦")
        await expect(page.locator("#cost")).to_contain_text("$3.60")
        await expect(
            page.locator('[data-job="Chat/synthetic-chat-a"]')
        ).to_contain_text("COMPLETED")
        checks.append("late snapshot and terminal state cannot regress")
        partial = snapshot()
        partial.update(
            jobs=None,
            activeCounts=None,
            chatCounts=None,
            profileCounts=None,
            collectionStatus="partial",
            dbError="synthetic unavailable",
        )
        partial["services"]["profiling"]["status"] = "unready"
        state["snapshot"] = partial
        await page.click("#refresh-dashboard")
        await expect(page.locator(".job-empty")).to_contain_text("수집하지 못")
        await expect(page.locator("#cost")).to_contain_text("$3.60")
        await expect(page.locator('[data-service="profiling"]')).to_contain_text(
            "준비 안 됨"
        )
        checks.append(
            "DB outage does not erase healthy central budget; component failure separate"
        )
        empty = snapshot()
        empty.update(
            jobs=[],
            activeCounts={"chat": 0, "profiling": 0},
            chatCounts={},
            profileCounts={},
        )
        empty["sharedBudget"] = {}
        state["snapshot"] = empty
        await page.click("#refresh-dashboard")
        await expect(page.locator(".job-empty")).to_contain_text(
            "관측된 작업이 없습니다"
        )
        await expect(page.locator("#budget-remaining")).to_have_text("알 수 없음")
        assert "$undefined" not in await page.locator("body").inner_text()
        checks.append("empty and unknown ledger are not fabricated success or zero")
        for _ in range(3):
            for tab in ["chat", "network", "profile", "dashboard"]:
                await page.click('[data-page="' + tab + '"]')
        checks.append("repeat navigation keeps one coherent view")
        await page.click('[data-page="chat"]')
        await page.fill("#message", "합성 요청 · 세 후보")
        await page.click("#send-chat")
        await expect(page.locator("#chat-status")).to_contain_text("429")
        await page.click("#retry-chat")
        await expect(page.locator("#chat-status")).to_contain_text("503")
        await page.click("#retry-chat")
        await expect(page.locator("#chat-status")).to_have_text("PROCESSING")
        assert (
            state["postBodies"][0] == state["postBodies"][1] == state["postBodies"][2]
        )
        checks.append("same UUID/body retry and upstream429503 visible")
        state["streamMode"] = "hold"
        await page.click("#reconnect")
        await expect(page.locator("#disconnect")).to_be_enabled()
        await page.click("#disconnect")
        await expect(page.locator("#connection-state")).to_contain_text("끊")
        checks.append("disconnect does not cancel server work")
        state["streamGate"].set()
        state["streamMode"] = "complete"
        await page.click("#reconnect")
        await expect(page.locator("#chat-status")).to_have_text("COMPLETED")
        elapsed = await page.locator("#elapsed").inner_text()
        await page.wait_for_timeout(250)
        assert elapsed == await page.locator("#elapsed").inner_text()
        assert any("afterEventId=1" in path for path in state["getPaths"])
        checks.append(
            "cursor reconnect, three-card preview and late processing GET cannot undo terminal"
        )
        await expect(page.locator("#event-list")).to_contain_text("#103")
        await page.click("#new-chat")
        await expect(page.locator("#event-preview-detail")).to_contain_text(
            "이벤트를 선택"
        )
        state["postCodes"] = [202]
        state["streamMode"] = "hold"
        state["streamGate"] = asyncio.Event()
        await page.click("#send-chat")
        await expect(page.locator("#chat-status")).to_have_text("PROCESSING")
        await page.click("#cancel-chat")
        await expect(page.locator("#chat-status")).to_have_text("CANCELLED")
        cancelled_elapsed = await page.locator("#elapsed").inner_text()
        await page.wait_for_timeout(250)
        assert cancelled_elapsed == await page.locator("#elapsed").inner_text()
        state["streamGate"].set()
        checks.append(
            "new request clears old preview; cancellation stays terminal after old state reply"
        )
        await page.check("#reveal")
        await page.locator(".raw-inspect summary").click()
        await page.click('[data-page="network"]')
        await page.check("#http-errors")
        for detail in await page.locator(".httprow details").all():
            await detail.locator("summary").click()
        assert "PRIVATE_" not in await page.locator("body").text_content()
        checks.append(
            "HTTP raw expansion never reveals secret error/prompt/provider/callback canaries"
        )
        await page.click('[data-page="dashboard"]')
        state["snapshot"] = snapshot()
        state["snapshot"]["jobs"] = []
        await page.click("#refresh-dashboard")
        await page.set_viewport_size({"width": 390, "height": 844})
        await page.wait_for_timeout(200)
        assert await page.evaluate("document.documentElement.scrollWidth<=innerWidth+2")
        await page.screenshot(
            path=str(args.output / "dashboard-mobile.png"), full_page=True
        )
        checks.append("mobile no page overflow")
        assert not errors, errors
        assert not outside, outside
        result = {
            "url": base,
            "checks": checks,
            "passed": True,
            "browser": "existing Chrome via Playwright",
            "apiMutations": "intercepted synthetic only",
            "newPaidCalls": 0,
            "pageErrors": errors,
            "outsideRequests": outside,
            "interceptedMutations": state["mutations"],
        }
        (args.output / "browser-result.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2)
        )
        print(json.dumps(result, ensure_ascii=False, indent=2))
        await browser.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
