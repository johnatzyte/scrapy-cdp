from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from websockets.asyncio.server import ServerConnection, serve

from scrapy_cdp.connection import CDPConnection
from scrapy_cdp.errors import CDPProtocolError


async def test_connection_multiplexes_commands_and_events() -> None:
    async def handler(socket: ServerConnection) -> None:
        first = json.loads(await socket.recv())
        second = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "method": "Page.lifecycleEvent",
                    "sessionId": "session-1",
                    "params": {"name": "load"},
                }
            )
        )
        await socket.send(json.dumps({"id": second["id"], "result": {"value": 2}}))
        await socket.send(json.dumps({"id": first["id"], "result": {"value": 1}}))

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connection = CDPConnection(f"ws://127.0.0.1:{port}", 1)
    events = connection.subscribe("session-1")
    try:
        first, second = await asyncio.gather(
            connection.command("First.command"),
            connection.command("Second.command"),
        )
        assert first == {"value": 1}
        assert second == {"value": 2}
        assert (await events.get())["params"] == {"name": "load"}
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()


async def test_connection_raises_protocol_errors() -> None:
    async def handler(socket: ServerConnection) -> None:
        command: dict[str, Any] = json.loads(await socket.recv())
        await socket.send(
            json.dumps(
                {
                    "id": command["id"],
                    "error": {"code": -32601, "message": "Unknown command"},
                }
            )
        )

    server = await serve(handler, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    connection = CDPConnection(f"ws://127.0.0.1:{port}", 1)
    try:
        with pytest.raises(CDPProtocolError, match="Unknown command"):
            await connection.command("Missing.command")
    finally:
        await connection.close()
        server.close()
        await server.wait_closed()
