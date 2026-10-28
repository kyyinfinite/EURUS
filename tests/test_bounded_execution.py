"""C4 regression tests: sandbox execution must stream output with a memory bound.

Before the fix, ``HostSandbox.execute`` (and the podman sandbox) awaited
``process.communicate()``, which buffers the ENTIRE stdout/stderr in memory
before truncating to the limit, and only kept the head of the output.
"""

from __future__ import annotations

import sys

import pytest

from eurus.execution.host import HostSandbox
from eurus.execution.sandbox import ExecutionRequest


def _request(argv: list[str], *, timeout: float = 30.0, limit: int = 20000) -> ExecutionRequest:
    return ExecutionRequest(
        argv=argv, cwd="/workspace", timeout_seconds=timeout, max_output_bytes=limit
    )


@pytest.mark.asyncio
async def test_small_output_is_returned_verbatim(tmp_path):
    sandbox = HostSandbox(tmp_path)
    result = await sandbox.execute(_request([sys.executable, "-c", "print('hello world')"]))
    assert result.exit_code == 0
    assert result.stdout == "hello world\n"
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_huge_output_is_bounded_and_keeps_head_and_tail(tmp_path):
    sandbox = HostSandbox(tmp_path)
    script = (
        "import sys\n"
        "sys.stdout.write('HEAD' + 'x' * (4 * 1024 * 1024))\n"
        "sys.stdout.write('TAIL_MARKER_END')\n"
        "sys.stdout.flush()\n"
    )
    result = await sandbox.execute(_request([sys.executable, "-c", script], limit=20000))
    assert result.exit_code == 0
    assert len(result.stdout) <= 20000 + 200, "output must stay within the byte limit"
    assert result.stdout.startswith("HEAD"), "head of the output must be preserved"
    assert result.stdout.endswith("TAIL_MARKER_END"), "tail of the output must be preserved"
    assert "truncated" in result.stdout.lower(), "truncation must be marked in the output"


@pytest.mark.asyncio
async def test_timeout_still_reports_partial_bounded_output(tmp_path):
    sandbox = HostSandbox(tmp_path)
    script = "import sys, time\nsys.stdout.write('PARTIAL')\nsys.stdout.flush()\ntime.sleep(10)\n"
    result = await sandbox.execute(
        _request([sys.executable, "-c", script], timeout=0.5, limit=20000)
    )
    assert result.timed_out is True
    assert result.exit_code == 124
    assert "PARTIAL" in result.stdout


@pytest.mark.asyncio
async def test_stderr_is_bounded_too(tmp_path):
    sandbox = HostSandbox(tmp_path)
    script = (
        "import sys\n"
        "sys.stderr.write('E' * (2 * 1024 * 1024))\n"
        "sys.stderr.write('STDERR_TAIL')\n"
        "sys.stderr.flush()\n"
    )
    result = await sandbox.execute(_request([sys.executable, "-c", script], limit=20000))
    assert len(result.stderr) <= 20000 + 200
    assert result.stderr.startswith("E")
    assert result.stderr.endswith("STDERR_TAIL")


def test_read_bounded_helper_produces_head_and_tail():
    from eurus.execution.bounded import BoundedOutput, read_bounded

    class FakeStream:
        def __init__(self, chunks: list[bytes]) -> None:
            self._chunks = chunks
            self.read_calls = 0

        async def read(self, size: int = -1) -> bytes:
            self.read_calls += 1
            if not self._chunks:
                return b""
            return self._chunks.pop(0)

    import asyncio

    total = 2 * 1024 * 1024
    chunk = b"A" * 65536
    stream = FakeStream([chunk] * (total // len(chunk)))
    output: BoundedOutput = asyncio.run(read_bounded(stream, limit=1000))
    assert output.truncated is True
    assert output.total_bytes == total
    text = output.text
    assert len(text) <= 1000 + 200
    assert "truncated" in text.lower()
    assert text.startswith("A")
    assert text.endswith("A")
    assert stream.read_calls > 10, "the reader must consume the stream in chunks"


def test_read_bounded_helper_small_stream_untruncated():
    from eurus.execution.bounded import read_bounded

    class FakeStream:
        def __init__(self, data: bytes) -> None:
            self._data = data

        async def read(self, size: int = -1) -> bytes:
            data, self._data = self._data[:size], self._data[size:]
            return data

    import asyncio

    output = asyncio.run(read_bounded(FakeStream(b"hello world"), limit=1000))
    assert output.text == "hello world"
    assert output.truncated is False
    assert output.total_bytes == 11
