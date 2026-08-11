"""Crawler lifecycle integration for the shared CDP service."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

from scrapy import signals
from scrapy.exceptions import NotConfigured
from scrapy.utils.reactor import is_asyncio_reactor_installed, is_reactor_installed

from scrapy_cdp.service import CDPService

if TYPE_CHECKING:
    from scrapy.crawler import Crawler

_CRAWLER_ATTRIBUTE: Final = "_scrapy_cdp_extension"


class CDPExtension:
    """Own the browser connection for one crawler."""

    def __init__(self, crawler: Crawler) -> None:
        if not crawler.settings.get("CDP_ENDPOINT"):
            raise NotConfigured("CDP_ENDPOINT must be configured")
        if is_reactor_installed() and not is_asyncio_reactor_installed():
            raise NotConfigured("scrapy-cdp requires the asyncio reactor")
        self._install_download_handlers(crawler)
        self.service = CDPService(crawler)
        setattr(crawler, _CRAWLER_ATTRIBUTE, self)
        crawler.signals.connect(self.close, signal=signals.engine_stopped)

    @classmethod
    def from_crawler(cls, crawler: Crawler) -> CDPExtension:
        return cls(crawler)

    async def close(self) -> None:
        await self.service.close()

    @staticmethod
    def _install_download_handlers(crawler: Crawler) -> None:
        settings = crawler.settings
        handlers = settings.getwithbase("DOWNLOAD_HANDLERS")
        settings.set(
            "SCRAPY_CDP_FALLBACK_HTTP_HANDLER",
            handlers["http"],
            priority="project",
        )
        settings.set(
            "SCRAPY_CDP_FALLBACK_HTTPS_HANDLER",
            handlers["https"],
            priority="project",
        )
        settings.set(
            "DOWNLOAD_HANDLERS",
            {
                "http": "scrapy_cdp.handler.CDPDownloadHandler",
                "https": "scrapy_cdp.handler.CDPDownloadHandler",
            },
            priority="project",
        )


def service_from_crawler(crawler: Crawler) -> CDPService:
    try:
        extension: CDPExtension = getattr(crawler, _CRAWLER_ATTRIBUTE)
    except AttributeError as exc:
        raise NotConfigured(
            "CDPExtension is not enabled; install scrapy_cdp.addon.ScrapyCDPAddon"
        ) from exc
    return extension.service
