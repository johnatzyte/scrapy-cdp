"""Scrapy add-on registration."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scrapy.settings import BaseSettings


class ScrapyCDPAddon:
    """Install the CDP extension and wrapping HTTP download handlers."""

    def update_settings(self, settings: BaseSettings) -> None:
        settings.set(
            "EXTENSIONS",
            {"scrapy_cdp.extension.CDPExtension": 500},
            priority="addon",
        )
