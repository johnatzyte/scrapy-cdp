from __future__ import annotations

import json
from collections import Counter
from typing import Any

from scrapy.settings import Settings
from websockets.asyncio.server import Server, ServerConnection, serve


class FakeStats:
    def __init__(self) -> None:
        self.values: Counter[str] = Counter()

    def inc_value(self, key: str, count: int = 1, **kwargs: Any) -> None:
        self.values[key] += count


class FakeCrawler:
    def __init__(self, **settings: Any) -> None:
        self.settings = Settings(settings)
        self.stats = FakeStats()


class FakeCDPBrowser:
    def __init__(self, *, send_lifecycle: bool = True) -> None:
        self.send_lifecycle = send_lifecycle
        self.commands: list[dict[str, Any]] = []
        self._server: Server | None = None
        self.endpoint = ""
        self._target_number = 0

    async def __aenter__(self) -> FakeCDPBrowser:
        self._server = await serve(self._handle, "127.0.0.1", 0)
        port = self._server.sockets[0].getsockname()[1]
        self.endpoint = f"ws://127.0.0.1:{port}"
        return self

    async def __aexit__(self, *args: object) -> None:
        assert self._server is not None
        self._server.close()
        await self._server.wait_closed()

    def count(self, method: str) -> int:
        return sum(command["method"] == method for command in self.commands)

    async def _handle(self, socket: ServerConnection) -> None:
        async for raw_message in socket:
            command = json.loads(raw_message)
            self.commands.append(command)
            method = command["method"]
            result: dict[str, Any] = {}
            if method == "Target.createBrowserContext":
                result = {"browserContextId": "context-1"}
            elif method == "Target.createTarget":
                self._target_number += 1
                result = {"targetId": f"target-{self._target_number}"}
            elif method == "Target.attachToTarget":
                result = {"sessionId": f"session-{self._target_number}"}
            elif method == "Page.navigate":
                result = {"frameId": "frame-1", "loaderId": "loader-1"}
            elif method == "Page.getFrameTree":
                result = {
                    "frameTree": {
                        "frame": {
                            "id": "frame-1",
                            "url": "https://example.com/final",
                        }
                    }
                }
            elif method == "DOM.getDocument":
                result = {"root": {"nodeId": 1}}
            elif method == "DOM.getOuterHTML":
                result = {
                    "outerHTML": (
                        "<!doctype html><html><body><p>rendered</p></body></html>"
                    )
                }

            response: dict[str, Any] = {"id": command["id"], "result": result}
            if session_id := command.get("sessionId"):
                response["sessionId"] = session_id
            await socket.send(json.dumps(response))

            if method == "Page.navigate" and self.send_lifecycle:
                await self._send_navigation_events(socket, command["sessionId"])

    async def _send_navigation_events(
        self, socket: ServerConnection, session_id: str
    ) -> None:
        await socket.send(
            json.dumps(
                {
                    "method": "Network.responseReceived",
                    "sessionId": session_id,
                    "params": {
                        "frameId": "frame-1",
                        "loaderId": "loader-1",
                        "type": "Document",
                        "response": {
                            "url": "https://example.com/final",
                            "status": 201,
                            "headers": {
                                "Content-Encoding": "gzip",
                                "Content-Length": "20",
                                "X-Test": "present",
                            },
                        },
                    },
                }
            )
        )
        await socket.send(
            json.dumps(
                {
                    "method": "Page.lifecycleEvent",
                    "sessionId": session_id,
                    "params": {
                        "frameId": "frame-1",
                        "loaderId": "loader-1",
                        "name": "load",
                    },
                }
            )
        )
