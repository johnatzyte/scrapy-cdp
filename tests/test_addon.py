from scrapy.settings import Settings

from scrapy_cdp.addon import ScrapyCDPAddon


def test_addon_installs_extension() -> None:
    settings = Settings()

    ScrapyCDPAddon().update_settings(settings)

    extensions = settings.getwithbase("EXTENSIONS")
    assert extensions["scrapy_cdp.extension.CDPExtension"] == 500
