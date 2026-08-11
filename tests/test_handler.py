from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

from scrapy import Request
from scrapy.http import TextResponse
from scrapy.settings import Settings

from scrapy_cdp.handler import CDPDownloadHandler

if TYPE_CHECKING:
    from scrapy.crawler import Crawler


class FallbackHandler:
    def __init__(self, crawler: Crawler) -> None:
        self.closed = False

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> FallbackHandler:
        return cls(crawler)

    async def download_request(self, request: Request) -> TextResponse:
        return TextResponse(request.url, body=b"fallback", request=request)

    async def close(self) -> None:
        self.closed = True


def make_crawler(service: AsyncMock) -> SimpleNamespace:
    return SimpleNamespace(
        settings=Settings(
            {
                "SCRAPY_CDP_FALLBACK_HTTP_HANDLER": FallbackHandler,
                "SCRAPY_CDP_FALLBACK_HTTPS_HANDLER": FallbackHandler,
            }
        ),
        spider=object(),
        _scrapy_cdp_extension=SimpleNamespace(service=service),
    )


async def test_handler_delegates_unmarked_requests() -> None:
    service = AsyncMock()
    handler = CDPDownloadHandler(make_crawler(service))  # type: ignore[arg-type]

    response = await handler.download_request(Request("https://example.com"))

    assert response.text == "fallback"
    service.render.assert_not_awaited()
    fallback = handler._fallbacks["https"]
    await handler.close()
    assert fallback.closed


async def test_handler_renders_marked_requests() -> None:
    request = Request("https://example.com", meta={"cdp": True})
    rendered = TextResponse(request.url, body=b"rendered", request=request)
    service = AsyncMock()
    service.render.return_value = rendered
    handler = CDPDownloadHandler(make_crawler(service))  # type: ignore[arg-type]

    response = await handler.download_request(request)

    assert response is rendered
    service.render.assert_awaited_once_with(request)
    assert not handler._fallbacks
