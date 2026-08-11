"""Crawler-scoped browser rendering service."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from time import monotonic
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
from urllib.request import urlopen

from scrapy.exceptions import DownloadFailedError, DownloadTimeoutError, NotSupported
from scrapy.http import Headers, HtmlResponse

from scrapy_cdp.connection import CDPConnection, Event
from scrapy_cdp.errors import CDPError

if TYPE_CHECKING:
    from scrapy import Request
    from scrapy.crawler import Crawler

_WAIT_EVENTS = {
    "domcontentloaded": "DOMContentLoaded",
    "load": "load",
    "none": None,
}


class CDPService:
    """Render opted-in requests in one crawler-owned browser context."""

    def __init__(self, crawler: Crawler) -> None:
        self.crawler = crawler
        settings = crawler.settings
        self.endpoint = settings.get("CDP_ENDPOINT")
        self.connect_timeout = settings.getfloat("CDP_CONNECT_TIMEOUT", 10.0)
        self.request_timeout = settings.getfloat("CDP_REQUEST_TIMEOUT", 30.0)
        self.default_wait_until = settings.get("CDP_WAIT_UNTIL", "load").lower()
        self.max_targets = settings.getint("CDP_MAX_TARGETS", 8)
        if self.default_wait_until not in _WAIT_EVENTS:
            raise ValueError(
                "CDP_WAIT_UNTIL must be 'domcontentloaded', 'load', or 'none'"
            )
        if self.max_targets < 1:
            raise ValueError("CDP_MAX_TARGETS must be at least 1")

        self._connection: CDPConnection | None = None
        self._resolved_endpoint: str | None = None
        self._context_id: str | None = None
        self._context_generation = 0
        self._start_lock = asyncio.Lock()
        self._targets = asyncio.Semaphore(self.max_targets)
        self._closed = False

    async def render(self, request: Request) -> HtmlResponse:
        if request.method != "GET":
            raise NotSupported("scrapy-cdp only supports GET requests")
        if self._closed:
            raise DownloadFailedError("scrapy-cdp is closed")

        wait_until = str(
            request.meta.get("cdp_wait_until", self.default_wait_until)
        ).lower()
        if wait_until not in _WAIT_EVENTS:
            raise ValueError(
                "cdp_wait_until must be 'domcontentloaded', 'load', or 'none'"
            )
        timeout = float(request.meta.get("cdp_timeout", self.request_timeout))
        started = monotonic()
        self.crawler.stats.inc_value("scrapy_cdp/request_count")

        try:
            async with asyncio.timeout(timeout):
                async with self._targets:
                    response = await self._render(request, wait_until)
        except TimeoutError as exc:
            self.crawler.stats.inc_value("scrapy_cdp/timeout_count")
            raise DownloadTimeoutError(
                f"CDP request timed out after {timeout:g} seconds: {request.url}"
            ) from exc
        except (CDPError, DownloadFailedError) as exc:
            self.crawler.stats.inc_value("scrapy_cdp/error_count")
            if isinstance(exc, DownloadFailedError):
                raise
            raise DownloadFailedError(str(exc)) from exc
        finally:
            request.meta["download_latency"] = monotonic() - started

        self.crawler.stats.inc_value("scrapy_cdp/response_count")
        return response

    async def close(self) -> None:
        self._closed = True
        connection = self._connection
        if connection is None:
            return
        if (
            self._context_id is not None
            and connection.generation == self._context_generation
        ):
            with suppress(CDPError, TimeoutError):
                async with asyncio.timeout(2):
                    await connection.command(
                        "Target.disposeBrowserContext",
                        {"browserContextId": self._context_id},
                    )
        self._context_id = None
        await connection.close()

    async def _render(self, request: Request, wait_until: str) -> HtmlResponse:
        connection, context_id = await self._ensure_started()
        target_id: str | None = None
        session_id: str | None = None
        events: asyncio.Queue[Event] | None = None
        try:
            target = await connection.command(
                "Target.createTarget",
                {"url": "about:blank", "browserContextId": context_id},
            )
            target_id = target["targetId"]
            self.crawler.stats.inc_value("scrapy_cdp/target_count")
            attached = await connection.command(
                "Target.attachToTarget",
                {"targetId": target_id, "flatten": True},
            )
            session_id = attached["sessionId"]
            events = connection.subscribe(session_id)

            for method, params in (
                ("Page.enable", None),
                ("Network.enable", None),
                ("DOM.enable", None),
                ("Page.setLifecycleEventsEnabled", {"enabled": True}),
            ):
                await connection.command(method, params, session_id=session_id)

            navigation = await connection.command(
                "Page.navigate", {"url": request.url}, session_id=session_id
            )
            if error_text := navigation.get("errorText"):
                raise DownloadFailedError(
                    f"CDP navigation failed for {request.url}: {error_text}"
                )

            frame_id = navigation["frameId"]
            loader_id = navigation.get("loaderId")
            document_response = await self._wait_for_navigation(
                events,
                frame_id=frame_id,
                loader_id=loader_id,
                lifecycle_event=_WAIT_EVENTS[wait_until],
            )
            frame_tree = await connection.command(
                "Page.getFrameTree", session_id=session_id
            )
            final_url = frame_tree["frameTree"]["frame"].get("url") or request.url
            document = await connection.command(
                "DOM.getDocument", {"depth": 0}, session_id=session_id
            )
            outer_html = await connection.command(
                "DOM.getOuterHTML",
                {"nodeId": document["root"]["nodeId"]},
                session_id=session_id,
            )
            return self._response(
                request,
                final_url,
                outer_html["outerHTML"],
                document_response,
            )
        finally:
            if session_id is not None and events is not None:
                connection.unsubscribe(session_id, events)
            if target_id is not None:
                await self._close_target(connection, target_id)

    async def _ensure_started(self) -> tuple[CDPConnection, str]:
        async with self._start_lock:
            if self._connection is None:
                endpoint = await self._resolve_endpoint()
                self._connection = CDPConnection(endpoint, self.connect_timeout)
            connection = self._connection
            await connection.connect()
            if (
                self._context_id is None
                or self._context_generation != connection.generation
            ):
                context = await connection.command("Target.createBrowserContext")
                self._context_id = context["browserContextId"]
                self._context_generation = connection.generation
            return connection, self._context_id

    async def _resolve_endpoint(self) -> str:
        if self._resolved_endpoint is not None:
            return self._resolved_endpoint
        if not self.endpoint:
            raise CDPError("CDP_ENDPOINT must be configured")
        parsed = urlparse(self.endpoint)
        if parsed.scheme in {"ws", "wss"}:
            self._resolved_endpoint = self.endpoint
            return self.endpoint
        if parsed.scheme not in {"http", "https"}:
            raise CDPError("CDP_ENDPOINT must use http, https, ws, or wss")

        version_url = self.endpoint
        if not parsed.path.rstrip("/").endswith("/json/version"):
            version_url = f"{self.endpoint.rstrip('/')}/json/version"

        def fetch_version() -> str:
            with urlopen(version_url, timeout=self.connect_timeout) as response:
                payload = json.load(response)
            try:
                return payload["webSocketDebuggerUrl"]
            except (KeyError, TypeError) as exc:
                raise CDPError(
                    f"{version_url} did not return webSocketDebuggerUrl"
                ) from exc

        try:
            self._resolved_endpoint = await asyncio.to_thread(fetch_version)
        except CDPError:
            raise
        except Exception as exc:
            raise CDPError(
                f"Could not discover CDP endpoint at {version_url}: {exc}"
            ) from exc
        return self._resolved_endpoint

    async def _wait_for_navigation(
        self,
        events: asyncio.Queue[Event],
        *,
        frame_id: str,
        loader_id: str | None,
        lifecycle_event: str | None,
    ) -> dict[str, Any] | None:
        document_response: dict[str, Any] | None = None
        while True:
            if lifecycle_event is None:
                try:
                    event = events.get_nowait()
                except asyncio.QueueEmpty:
                    return document_response
            else:
                event = await events.get()

            method = event.get("method")
            params = event.get("params", {})
            if (
                method == "Network.responseReceived"
                and params.get("type") == "Document"
            ):
                if params.get("frameId") == frame_id and (
                    loader_id is None or params.get("loaderId") == loader_id
                ):
                    document_response = params.get("response")
            elif method == "Network.loadingFailed" and (
                loader_id is None or params.get("loaderId") == loader_id
            ):
                raise DownloadFailedError(
                    f"CDP navigation failed: {params.get('errorText', 'unknown error')}"
                )
            elif method == "Page.lifecycleEvent" and (
                params.get("frameId") == frame_id
                and (loader_id is None or params.get("loaderId") == loader_id)
                and params.get("name") == lifecycle_event
            ):
                return document_response

    async def _close_target(self, connection: CDPConnection, target_id: str) -> None:
        with suppress(CDPError, TimeoutError):
            async with asyncio.timeout(2):
                await asyncio.shield(
                    connection.command("Target.closeTarget", {"targetId": target_id})
                )

    @staticmethod
    def _response(
        request: Request,
        final_url: str,
        html: str,
        document_response: dict[str, Any] | None,
    ) -> HtmlResponse:
        response_data = document_response or {}
        headers = Headers(
            {
                str(name): str(value)
                for name, value in response_data.get("headers", {}).items()
            }
        )
        headers.pop("Content-Encoding", None)
        headers.pop("Content-Length", None)
        headers["Content-Type"] = "text/html; charset=utf-8"
        return HtmlResponse(
            url=final_url,
            status=int(response_data.get("status", 200)),
            headers=headers,
            body=html.encode(),
            encoding="utf-8",
            request=request,
            flags=["cdp"],
        )
