from __future__ import annotations

import scrapy
from scrapy.crawler import AsyncCrawlerRunner

from scrapy_cdp.addon import ScrapyCDPAddon
from tests.helpers import FakeCDPBrowser


async def test_addon_runs_inside_scrapy_request_cycle() -> None:
    responses: list[scrapy.http.Response] = []

    class IntegrationSpider(scrapy.Spider):
        name = "cdp-integration"

        async def start(self):
            yield scrapy.Request("https://example.com", meta={"cdp": True})

        def parse(self, response: scrapy.http.Response) -> None:
            responses.append(response)

    async with FakeCDPBrowser() as browser:
        runner = AsyncCrawlerRunner(
            {
                "ADDONS": {ScrapyCDPAddon: 500},
                "CDP_ENDPOINT": browser.endpoint,
                "LOG_ENABLED": False,
                "TWISTED_REACTOR_ENABLED": False,
            }
        )
        await runner.crawl(IntegrationSpider)

    assert len(responses) == 1
    assert responses[0].url == "https://example.com/final"
    assert responses[0].css("p::text").get() == "rendered"
    assert responses[0].flags == ["cdp"]
