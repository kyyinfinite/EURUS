import asyncio
import contextlib
import json
import sys
import time
import tracemalloc
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from eurus.catalog import BUILTIN_PROVIDERS
from eurus.models import Message, ModelRef
from eurus.providers.base import RetryPolicy
from eurus.providers.openai_compat import OpenAICompatProvider
from eurus.providers.router import Router
from eurus.providers.testing import RecordingSleep
from eurus.providers.transport import HttpError

MESSAGES = [Message.user("ping")]
OK = {"choices": [{"message": {"content": "pong"}, "finish_reason": "stop"}]}


def chunk(text):
    return json.dumps({"choices": [{"delta": {"content": text}}]})


class LoopTransport:
    def __init__(self, mode="ok"):
        self.mode = mode
        self.calls = 0
        self.closed_streams = 0

    async def post_json(self, url, headers, payload):
        self.calls += 1
        if self.mode == "rate_limited":
            raise HttpError(429, "{}", 1.0)
        return OK

    async def stream_sse(self, url, headers, payload):
        self.calls += 1
        try:
            if self.mode == "rate_limited":
                raise HttpError(429, "{}", 1.0)
            yield chunk("hello ")
            yield chunk("world")
            yield "[DONE]"
        finally:
            self.closed_streams += 1

    async def aclose(self):
        return None


def build(mode_a, mode_b, **kwargs):
    a = LoopTransport(mode_a)
    b = LoopTransport(mode_b)
    sleep = RecordingSleep()
    providers = {}
    for name, transport in (("groq", a), ("openai", b)):
        providers[name] = OpenAICompatProvider(
            BUILTIN_PROVIDERS[name],
            "bench-key",
            transport,
            RetryPolicy(max_retries=0),
            sleep,
        )
    router = Router(
        providers,
        [ModelRef("groq", "demo"), ModelRef("openai", "demo")],
        **kwargs,
    )
    return router, a, b


async def chat_many(router, count):
    for _ in range(count):
        await router.chat(MESSAGES)


async def stream_first_token(router, count):
    total = 0.0
    for _ in range(count):
        started = time.perf_counter()
        async for event in router.stream(MESSAGES):
            if event.kind == "text":
                total += time.perf_counter() - started
                break
    return total


async def drain_stream(router, count):
    for _ in range(count):
        async for _ in router.stream(MESSAGES):
            pass


async def concurrency_probe(count):
    router, a, b = build("ok", "ok")
    await asyncio.gather(*(router.chat(MESSAGES) for _ in range(count)))
    return router


async def cancellation_probe():
    router, a, b = build("ok", "ok")
    task = asyncio.create_task(drain_stream(router, 1))
    await asyncio.sleep(0.001)
    task.cancel()
    started = time.perf_counter()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    return time.perf_counter() - started, a.closed_streams + b.closed_streams


def bench(name, rounds, fn):
    runs = []
    for _ in range(rounds):
        started = time.perf_counter()
        result = asyncio.run(fn())
        runs.append((time.perf_counter() - started, result))
    best = min(runs, key=lambda item: item[0])
    median = sorted(runs[len(runs) // 2 - 1 : len(runs) // 2 + 1])[0][0]
    print(f"{name:42s} best={best[0] * 1000:9.3f}ms median={median * 1000:9.3f}ms {best[1]}")


async def routing_no_fallback(count=500):
    router, a, b = build("ok", "ok")
    started = time.perf_counter()
    await chat_many(router, count)
    elapsed = time.perf_counter() - started
    return f"calls={a.calls} fallbacks=0 per_req={elapsed / count * 1e6:.1f}us"


async def routing_with_fallback(count=500):
    router, a, b = build("rate_limited", "ok")
    started = time.perf_counter()
    await chat_many(router, count)
    elapsed = time.perf_counter() - started
    return f"upstream_calls={a.calls + b.calls} per_req={elapsed / count * 1e6:.1f}us"


async def cooldown_overhead(count=2000):
    router, a, b = build("ok", "ok")
    router.penalize_provider("groq", retry_after=None)
    started = time.perf_counter()
    await chat_many(router, count)
    elapsed = time.perf_counter() - started
    return f"per_req={elapsed / count * 1e6:.1f}us cooling={len(router.provider_cooldowns())}"


async def ttft(count=300):
    router, a, b = build("ok", "ok")
    total = await stream_first_token(router, count)
    return f"mean_ttft={total / count * 1e6:.1f}us"


async def ttft_with_fallback(count=200):
    router, a, b = build("rate_limited", "ok")
    total = await stream_first_token(router, count)
    return f"mean_ttft={total / count * 1e6:.1f}us upstream_calls={a.calls + b.calls}"


async def full_stream(count=300):
    router, a, b = build("ok", "ok")
    started = time.perf_counter()
    async for _ in drain_stream(router, count):
        pass
    elapsed = time.perf_counter() - started
    return f"per_stream={elapsed / count * 1e6:.1f}us streams_closed={a.closed_streams}"


async def concurrent(count=64):
    started = time.perf_counter()
    await concurrency_probe(count)
    elapsed = time.perf_counter() - started
    return f"wall={elapsed * 1000:.2f}ms requests={count}"


async def cancel_cleanup():
    elapsed, closed = await cancellation_probe()
    return f"cleanup={elapsed * 1000:.3f}ms streams_closed={closed}"


async def memory_probe(count=1000):
    tracemalloc.start()
    router, a, b = build("rate_limited", "ok")
    await chat_many(router, count)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return f"current={current / 1024:.1f}KB peak={peak / 1024:.1f}KB requests={count}"


def main():
    print(f"python {sys.version.split()[0]} deterministic fake-provider benchmark")
    bench("routing latency without fallback (500)", 3, routing_no_fallback)
    bench("routing latency with fallback (500)", 3, routing_with_fallback)
    bench("cooldown filter overhead (2000)", 3, cooldown_overhead)
    bench("time to first token (300)", 3, ttft)
    bench("time to first token via fallback (200)", 3, ttft_with_fallback)
    bench("full stream drain (300)", 3, full_stream)
    bench("concurrent chat (64)", 5, concurrent)
    bench("cancellation cleanup", 5, lambda: cancel_cleanup())
    bench("memory during 1000 fallback requests", 2, memory_probe)


if __name__ == "__main__":
    main()
