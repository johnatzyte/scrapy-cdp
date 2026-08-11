"""Small asyncio CDP JSON-RPC transport."""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from contextlib import suppress
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from scrapy_cdp.errors import CDPConnectionError, CDPProtocolError

Event = dict[str, Any]


class CDPConnection:
    """Multiplex CDP commands and target events over one WebSocket."""

    def __init__(self, endpoint: str, connect_timeout: float) -> None:
        self.endpoint = endpoint
        self.connect_timeout = connect_timeout
        self.generation = 0
        self._socket: ClientConnection | None = None
        self._reader: asyncio.Task[None] | None = None
        self._connect_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._next_id = 0
        self._pending: dict[int, tuple[str, asyncio.Future[dict[str, Any]]]] = {}
        self._subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
        self._closed = False

    async def connect(self) -> None:
        if self._socket is not None:
            return
        if self._closed:
            raise CDPConnectionError("CDP connection is closed")

        async with self._connect_lock:
            if self._socket is not None:
                return
            try:
                socket = await asyncio.wait_for(
                    connect(self.endpoint, max_size=None),
                    timeout=self.connect_timeout,
                )
            except Exception as exc:
                raise CDPConnectionError(
                    f"Could not connect to CDP endpoint {self.endpoint}: {exc}"
                ) from exc
            self._socket = socket
            self.generation += 1
            self._reader = asyncio.create_task(self._read_messages(socket))

    async def command(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        await self.connect()
        loop = asyncio.get_running_loop()
        self._next_id += 1
        command_id = self._next_id
        future: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending[command_id] = (method, future)

        message: dict[str, Any] = {
            "id": command_id,
            "method": method,
            "params": params or {},
        }
        if session_id is not None:
            message["sessionId"] = session_id

        try:
            async with self._send_lock:
                if self._socket is None:
                    raise CDPConnectionError("CDP connection closed before send")
                await self._socket.send(json.dumps(message))
            return await future
        except asyncio.CancelledError:
            self._pending.pop(command_id, None)
            future.cancel()
            raise
        except Exception:
            self._pending.pop(command_id, None)
            future.cancel()
            raise

    def subscribe(self, session_id: str) -> asyncio.Queue[Event]:
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._subscribers[session_id].add(queue)
        return queue

    def unsubscribe(self, session_id: str, queue: asyncio.Queue[Event]) -> None:
        subscribers = self._subscribers.get(session_id)
        if subscribers is None:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._subscribers.pop(session_id, None)

    async def close(self) -> None:
        self._closed = True
        socket, self._socket = self._socket, None
        if socket is not None:
            await socket.close()
        if self._reader is not None and self._reader is not asyncio.current_task():
            with suppress(ConnectionClosed):
                await self._reader
        self._reader = None
        self._fail_pending(CDPConnectionError("CDP connection closed"))

    async def _read_messages(self, socket: ClientConnection) -> None:
        error: Exception = CDPConnectionError("Browser closed the CDP connection")
        try:
            async for raw_message in socket:
                message = json.loads(raw_message)
                if "id" in message:
                    self._resolve_command(message)
                elif session_id := message.get("sessionId"):
                    for queue in tuple(self._subscribers.get(session_id, ())):
                        queue.put_nowait(message)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            error = CDPConnectionError(f"CDP connection failed: {exc}")
        finally:
            if self._socket is socket:
                self._socket = None
            self._fail_pending(error)

    def _resolve_command(self, message: dict[str, Any]) -> None:
        pending = self._pending.pop(message["id"], None)
        if pending is None:
            return
        method, future = pending
        if future.done():
            return
        if error := message.get("error"):
            future.set_exception(
                CDPProtocolError(method, error.get("code"), error.get("message", ""))
            )
        else:
            future.set_result(message.get("result", {}))

    def _fail_pending(self, error: Exception) -> None:
        pending, self._pending = self._pending, {}
        for _, future in pending.values():
            if not future.done():
                future.set_exception(error)
