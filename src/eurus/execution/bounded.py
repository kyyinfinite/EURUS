from __future__ import annotations

import asyncio
from dataclasses import dataclass

CHUNK_SIZE = 65536
HEAD_SHARE = 0.7


@dataclass(frozen=True, slots=True)
class BoundedOutput:
    text: str
    total_bytes: int
    truncated: bool


async def read_bounded(stream: asyncio.StreamReader, limit: int) -> BoundedOutput:
    if limit < 1:
        raise ValueError("limit must be at least 1")
    head = bytearray()
    tail = bytearray()
    total = 0
    truncated = False
    while True:
        chunk = await stream.read(CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        room = limit - len(head)
        if room > 0:
            taken = chunk[:room]
            head.extend(taken)
            rest = chunk[len(taken) :]
        else:
            rest = chunk
        if rest:
            truncated = True
            tail.extend(rest)
            if len(tail) > limit:
                del tail[: len(tail) - limit]
    if not truncated:
        return BoundedOutput(bytes(head).decode("utf-8", errors="replace"), total, False)
    head_cap = max(1, int(limit * HEAD_SHARE))
    tail_cap = max(1, limit - head_cap)
    if len(head) > head_cap:
        del head[head_cap:]
    if len(tail) > tail_cap:
        del tail[: len(tail) - tail_cap]
    dropped = max(0, total - len(head) - len(tail))
    marker = f"\n... [{dropped} bytes truncated] ...\n"
    text = (
        bytes(head).decode("utf-8", errors="replace")
        + marker
        + bytes(tail).decode("utf-8", errors="replace")
    )
    return BoundedOutput(text, total, True)


async def spawn_readers(
    process: asyncio.subprocess.Process, limit: int
) -> tuple[asyncio.Task[BoundedOutput], asyncio.Task[BoundedOutput]]:

    async def read(stream: asyncio.StreamReader | None) -> BoundedOutput:
        if stream is None:
            return BoundedOutput("", 0, False)
        return await read_bounded(stream, limit)

    if process.stdout is None or process.stderr is None:
        raise RuntimeError("subprocess pipes are not captured")
    return (
        asyncio.create_task(read(process.stdout)),
        asyncio.create_task(read(process.stderr)),
    )
