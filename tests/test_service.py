from __future__ import annotations

import asyncio

import pytest
from scrapy import Request
from scrapy.exceptions import DownloadTimeoutError, NotSupported

from scrapy_cdp.service import CDPService
from tests.helpers import FakeCDPBrowser, FakeCrawler


async def test_service_returns_rendered_response_and_reuses_context() -> None:
    async with FakeCDPBrowser() as browser:
        crawler = FakeCrawler(CDP_ENDPOINT=browser.endpoint)
        service = CDPService(crawler)  # type: ignore[arg-type]

        first = await service.render(Request("https://example.com", meta={"cdp": True}))
        second = await service.render(
            Request("https://example.com/2", meta={"cdp": True})
        )
        await service.close()

    assert first.url == "https://example.com/final"
    assert first.status == 201
    assert first.css("p::text").get() == "rendered"
    assert first.flags == ["cdp"]
    assert first.headers["Content-Type"] == b"text/html; charset=utf-8"
    assert "Content-Encoding" not in first.headers
    assert "Content-Length" not in first.headers
    assert first.headers["X-Test"] == b"present"
    assert second.status == 201
    assert browser.count("Target.createBrowserContext") == 1
    assert browser.count("Target.createTarget") == 2
    assert browser.count("Target.closeTarget") == 2
    assert browser.count("Target.disposeBrowserContext") == 1
    assert crawler.stats.values["scrapy_cdp/request_count"] == 2
    assert crawler.stats.values["scrapy_cdp/response_count"] == 2


async def test_service_times_out_and_closes_target() -> None:
    async with FakeCDPBrowser(send_lifecycle=False) as browser:
        crawler = FakeCrawler(CDP_ENDPOINT=browser.endpoint, CDP_REQUEST_TIMEOUT=0.05)
        service = CDPService(crawler)  # type: ignore[arg-type]

        with pytest.raises(DownloadTimeoutError):
            await service.render(Request("https://example.com", meta={"cdp": True}))
        await service.close()

    assert browser.count("Target.closeTarget") == 1
    assert crawler.stats.values["scrapy_cdp/timeout_count"] == 1


async def test_service_limits_concurrent_targets() -> None:
    async with FakeCDPBrowser() as browser:
        crawler = FakeCrawler(CDP_ENDPOINT=browser.endpoint, CDP_MAX_TARGETS=1)
        service = CDPService(crawler)  # type: ignore[arg-type]

        await asyncio.gather(
            service.render(Request("https://example.com/1", meta={"cdp": True})),
            service.render(Request("https://example.com/2", meta={"cdp": True})),
        )
        await service.close()

    methods = [command["method"] for command in browser.commands]
    first_close = methods.index("Target.closeTarget")
    second_create = methods.index("Target.createTarget", first_close)
    assert first_close < second_create


async def test_service_rejects_non_get_requests() -> None:
    crawler = FakeCrawler(CDP_ENDPOINT="ws://127.0.0.1:1")
    service = CDPService(crawler)  # type: ignore[arg-type]

    with pytest.raises(NotSupported, match="only supports GET"):
        await service.render(Request("https://example.com", method="POST"))
