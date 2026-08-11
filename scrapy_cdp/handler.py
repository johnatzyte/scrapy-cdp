"""Opt-in CDP download handler with normal HTTP fallback."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING, Any

from scrapy.utils.defer import ensure_awaitable, maybe_deferred_to_future
from scrapy.utils.httpobj import urlparse_cached
from scrapy.utils.misc import build_from_crawler, load_object

from scrapy_cdp.extension import service_from_crawler

if TYPE_CHECKING:
    from scrapy import Request
    from scrapy.crawler import Crawler
    from scrapy.http import Response


class CDPDownloadHandler:
    """Route marked requests through CDP and delegate all other requests."""

    lazy = True

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        self.service = service_from_crawler(crawler)
        self._fallbacks: dict[str, Any] = {}

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CDPDownloadHandler:
        return cls(crawler)

    async def download_request(self, request: Request) -> Response:
        if request.meta.get("cdp"):
            return await self.service.render(request)
        fallback = self._fallback(urlparse_cached(request).scheme)
        if inspect.iscoroutinefunction(fallback.download_request):
            return await fallback.download_request(request)
        result = fallback.download_request(request, self.crawler.spider)
        return await maybe_deferred_to_future(result)

    async def close(self) -> None:
        for fallback in self._fallbacks.values():
            close = getattr(fallback, "close", None)
            if close is None:
                continue
            await ensure_awaitable(close())
        self._fallbacks.clear()

    def _fallback(self, scheme: str) -> Any:
        if scheme not in self._fallbacks:
            setting = f"SCRAPY_CDP_FALLBACK_{scheme.upper()}_HANDLER"
            handler_class = load_object(self.crawler.settings[setting])
            self._fallbacks[scheme] = build_from_crawler(handler_class, self.crawler)
        return self._fallbacks[scheme]
