"""Per-application WebSocket connections with bounded, independent delivery."""

import asyncio
import logging

from fastapi import WebSocket

logger = logging.getLogger(__name__)


class ConnectionManager:
    def __init__(self, *, queue_size: int = 64, send_timeout: float = 2.0):
        self._connections: dict[WebSocket, asyncio.Queue] = {}
        self._senders: dict[WebSocket, asyncio.Task] = {}
        self._queue_size = queue_size
        self._send_timeout = send_timeout

    @property
    def connection_count(self) -> int:
        return len(self._connections)

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._queue_size)
        self._connections[websocket] = queue
        self._senders[websocket] = asyncio.create_task(
            self._send_loop(websocket, queue)
        )

    async def _send_loop(self, websocket: WebSocket, queue: asyncio.Queue):
        try:
            while True:
                message = await queue.get()
                await asyncio.wait_for(websocket.send_json(message), self._send_timeout)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.debug("WebSocket sender disconnected", exc_info=True)
        finally:
            self._connections.pop(websocket, None)
            self._senders.pop(websocket, None)
            try:
                await asyncio.wait_for(websocket.close(), self._send_timeout)
            except Exception:
                pass

    async def send(self, websocket: WebSocket, message: dict):
        queue = self._connections.get(websocket)
        if queue is None:
            return
        try:
            queue.put_nowait(message)
        except asyncio.QueueFull:
            task = self._senders.get(websocket)
            if task:
                task.cancel()
            self._connections.pop(websocket, None)

    async def broadcast(self, message: dict):
        for websocket in list(self._connections):
            await self.send(websocket, message)

    async def disconnect(self, websocket: WebSocket):
        self._connections.pop(websocket, None)
        task = self._senders.pop(websocket, None)
        if task and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def close(self):
        tasks = list(self._senders.values())
        self._connections.clear()
        self._senders.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
