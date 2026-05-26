"""Length-prefixed JSON message framing over asyncio pipe streams.

Shared by the host (``secure.py``) and the sandboxed child (``_sandbox_child.py``). Each message
is a 4-byte big-endian length followed by UTF-8 JSON. The RPC channel rides on inherited pipe
file descriptors — verified to work under a deny-default Seatbelt profile (pipes aren't the
``network*`` class and fd I/O on already-open descriptors isn't path-checked).

This is the boundary that makes the security real: the untrusted script (in the sandbox) can
only reach the trusted host through this channel, and the only methods it exposes are the six
workflow primitives. No filesystem, no network — just orchestration requests.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any


def pack(obj: Any) -> bytes:
    body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    return len(body).to_bytes(4, "big") + body


async def read_msg(reader: asyncio.StreamReader) -> dict | None:
    """Read one framed message; return None on clean EOF."""
    try:
        header = await reader.readexactly(4)
    except asyncio.IncompleteReadError:
        return None
    n = int.from_bytes(header, "big")
    try:
        body = await reader.readexactly(n)
    except asyncio.IncompleteReadError:
        return None
    return json.loads(body.decode("utf-8"))


class Channel:
    """Async framed-message channel over a read fd and a write fd, with write serialization."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._wlock = asyncio.Lock()

    @classmethod
    async def from_fds(cls, read_fd: int, write_fd: int) -> "Channel":
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(read_fd, "rb", buffering=0)
        )
        w_transport, w_proto = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(write_fd, "wb", buffering=0)
        )
        writer = asyncio.StreamWriter(w_transport, w_proto, reader, loop)
        return cls(reader, writer)

    async def send(self, obj: Any) -> None:
        async with self._wlock:
            self._writer.write(pack(obj))
            await self._writer.drain()

    async def recv(self) -> dict | None:
        return await read_msg(self._reader)

    def close(self) -> None:
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001
            pass
