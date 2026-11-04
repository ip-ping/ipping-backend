"""Live node connections.

This registry is in-process memory. That is only correct while one backend process owns all
node connections; running several workers needs shared routing first (see the README).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from fastapi import WebSocket
from pydantic import BaseModel

from .protocol import Capabilities


@dataclass(eq=False)
class NodeConnection:
    node_id: str
    websocket: WebSocket
    protocol_version: int
    capabilities: Capabilities
    _send_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def send(self, frame: BaseModel, timeout: float = 5.0) -> None:
        async with self._send_lock:
            await asyncio.wait_for(self.websocket.send_text(frame.model_dump_json()), timeout)

    async def close(self, code: int, reason: str = "") -> None:
        try:
            await self.websocket.close(code=code, reason=reason)
        except Exception:  # noqa: BLE001, S110 - already closed or broken; nothing left to do
            pass


class NodeRegistry:
    def __init__(self) -> None:
        self._connections: dict[str, NodeConnection] = {}

    def register(self, connection: NodeConnection) -> NodeConnection | None:
        """Add a connection and return the one it replaced, if any."""
        previous = self._connections.get(connection.node_id)
        self._connections[connection.node_id] = connection
        return previous

    def unregister(self, connection: NodeConnection) -> bool:
        """Remove `connection` unless a newer one has already replaced it."""
        if self._connections.get(connection.node_id) is connection:
            del self._connections[connection.node_id]
            return True
        return False

    def get(self, node_id: str) -> NodeConnection | None:
        return self._connections.get(node_id)

    def online_ids(self) -> set[str]:
        return set(self._connections)

    def __len__(self) -> int:
        return len(self._connections)
