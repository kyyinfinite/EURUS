import asyncio
import json
import logging
import time

import pytest

from eurus.catalog import BUILTIN_PROVIDERS
from eurus.config import build_config, load_config
from eurus.errors import AllProvidersFailedError, ProviderUnavailableError
from eurus.models import Message, ModelRef
from eurus.providers.base import ChatRequest, ChatResponse, Provider, RetryPolicy, StreamEvent
from eurus.providers.factory import build_router
from eurus.providers.openai_compat import OpenAICompatProvider
from eurus.providers.router import Router
from eurus.providers.testing import FakeTransport, RecordingSleep
from eurus.providers.transport import HttpError, TransportError

MESSAGES = [Message.user("hi")]
REQUEST = ChatRequest(model="m", messages=MESSAGES, max_tokens=10, temperature=0.2)
OK = {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}


def chunk(text):
    return json.dumps({"choices": [{"delta": {"content": text}}]})


def make(name, transport, retries=0, retry=None):
    return OpenAICompatProvider(
        BUILTIN_PROVIDERS[name],
        "k",
        transport,
        retry or RetryPolicy(max_retries=retries),
        RecordingSleep(),
    )


def pair(a_transport, b_transport, **kwargs):
    return Router(
        {
            "groq": make("groq", a_transport),
            "openai": make("openai", b_transport),
        },
        [ModelRef("groq", "a"), ModelRef("openai", "b")],
        **kwargs,
    )


def run(coro):
    return asyncio.run(coro)


def collect_stream(router):
    async def go():
        return [event async for event in router.stream(MESSAGES)]

    return run(go())


class FakeClock:
    def __init__(self, step=0.0):
        self.now = 100.0
        self.step = step
        self.delays = []

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.delays.append(delay)
        self.now += delay + self.step


class ScriptedProvider(Provider):
    def __init__(self, name, chat=(), retry=None, clock=None, sleep=None):
        super().__init__(
            BUILTIN_PROVIDERS[name], "k", FakeTransport(), retry or RetryPolicy(max_retries=0)
        )
        self._clock = clock or time.monotonic
        if sleep is not None:
            self._sleep = sleep
        self.script = list(chat)
        self.calls = 0

    async def _chat(self, request):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        return ChatResponse(
            message=Message.assistant(str(item)), model=request.model, provider=self.name
        )

    async def _stream(self, request):
        self.calls += 1
        item = self.script.pop(0) if self.script else "ok"
        if isinstance(item, Exception):
            raise item
        yield StreamEvent.text_delta(str(item))
        yield StreamEvent.done("stop")


class HangingTransport(FakeTransport):
    def __init__(self):
        super().__init__()
        self.closed_streams = 0

    async def stream_sse(self, url, headers, payload):
        try:
            await asyncio.Event().wait()
            yield ""
        finally:
            self.closed_streams += 1


def test_provider_success_on_first_attempt():
    a = FakeTransport(json_responses=[OK])
    router = Router({"groq": make("groq", a)}, [ModelRef("groq", "a")])
    response = run(router.chat(MESSAGES))
    assert response.message.content == "ok"
    records = router.attempt_log()
    assert len(records) == 1
    assert records[0].outcome == "success"
    assert records[0].fallback is False


def test_rate_limit_before_first_token_falls_back():
    a = FakeTransport(streams=[HttpError(429, "{}", 2.0)])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    router = pair(a, b)
    events = collect_stream(router)
    assert events[0].text == "b-text"
    assert router.last_used == ModelRef("openai", "b")
    cooling = router.provider_cooldowns()
    assert cooling["groq"] > 0
    records = router.attempt_log()
    assert records[0].outcome == "failed"
    assert records[0].error_category == "rate_limit"
    assert records[-1].outcome == "success"
    assert records[-1].fallback is True


def test_stream_timeout_falls_back():
    a = FakeTransport(streams=[TransportError("request timed out")])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    events = collect_stream(pair(a, b))
    assert events[0].text == "b-text"


def test_temporary_server_error_retries_then_falls_back():
    a = FakeTransport(json_responses=[HttpError(500, "x"), HttpError(500, "x")])
    b = FakeTransport(json_responses=[OK])
    router = Router(
        {
            "groq": make("groq", a, retry=RetryPolicy(max_retries=1)),
            "openai": make("openai", b),
        },
        [ModelRef("groq", "a"), ModelRef("openai", "b")],
    )
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert len(a.calls) == 2
    assert len(b.calls) == 1


def test_chat_timeout_falls_back():
    a = FakeTransport(json_responses=[TransportError("request timed out")])
    b = FakeTransport(json_responses=[OK])
    assert run(pair(a, b).chat(MESSAGES)).message.content == "ok"


def test_auth_error_skips_remaining_candidates_of_same_provider():
    a = FakeTransport(json_responses=[HttpError(401, '{"error": {"message": "bad key"}}')])
    b = FakeTransport(json_responses=[OK])
    groq = make("groq", a)
    router = Router(
        {"groq": groq, "openai": make("openai", b)},
        [ModelRef("groq", "a"), ModelRef("groq", "b"), ModelRef("openai", "c")],
    )
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert len(a.calls) == 1
    assert len(b.calls) == 1
    outcomes = [record.outcome for record in router.attempt_log()]
    assert outcomes == ["failed", "skipped", "success"]
    assert router.attempt_log()[1].error_category == "authentication"


def test_stream_auth_error_skips_remaining_candidates_of_same_provider():
    a = FakeTransport(streams=[HttpError(403, "forbidden")])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    router = Router(
        {"groq": make("groq", a), "openai": make("openai", b)},
        [ModelRef("groq", "a"), ModelRef("groq", "b"), ModelRef("openai", "c")],
    )
    events = collect_stream(router)
    assert events[0].text == "b-text"
    assert len(a.calls) == 1


def test_invalid_request_does_not_penalize_provider():
    a = FakeTransport(json_responses=[HttpError(400, '{"error": {"message": "bad request"}}')])
    b = FakeTransport(json_responses=[OK])
    router = pair(a, b)
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert router.provider_cooldowns() == {}
    records = router.attempt_log()
    assert records[0].error_category == "invalid_request"
    assert records[0].outcome == "failed"


def test_invalid_request_still_tries_next_candidate():
    body = '{"error": {"message": "not found for account"}}'
    a = FakeTransport(json_responses=[HttpError(404, body)])
    b = FakeTransport(json_responses=[OK])
    assert run(pair(a, b).chat(MESSAGES)).message.content == "ok"


def test_cooldown_provider_is_skipped_when_alternative_healthy():
    a = FakeTransport(json_responses=[HttpError(500, "down")])
    b = FakeTransport(json_responses=[OK, OK])
    router = pair(a, b)
    run(router.chat(MESSAGES))
    assert router.provider_cooldowns()["groq"] > 0
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert len(a.calls) == 1
    assert len(b.calls) == 2
    assert router.last_used == ModelRef("openai", "b")


def test_all_cooling_still_serves_earliest_expiry():
    a = FakeTransport(json_responses=[HttpError(500, "x"), OK])
    b = FakeTransport(json_responses=[HttpError(500, "y")])
    router = pair(a, b)
    with pytest.raises(AllProvidersFailedError):
        run(router.chat(MESSAGES))
    assert set(router.provider_cooldowns()) == {"groq", "openai"}
    router.expire_cooldown("groq")
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert "groq" not in router.provider_cooldowns()
    assert "openai" in router.provider_cooldowns()


def test_all_cooling_ordered_by_shortest_cooldown():
    router = Router(
        {
            "groq": ScriptedProvider("groq"),
            "openai": ScriptedProvider("openai"),
        },
        [ModelRef("groq", "a"), ModelRef("openai", "b")],
        cooldown_seconds=1.0,
    )
    router.penalize_provider("groq", retry_after=2.0)
    router.penalize_provider("openai", retry_after=None)
    assert router._candidates()[0].provider == "openai"
    assert router._candidates()[1].provider == "groq"


def test_all_candidates_fail_keeps_original_errors():
    a = FakeTransport(json_responses=[HttpError(500, "first-down")])
    b = FakeTransport(json_responses=[HttpError(401, "bad key")])
    with pytest.raises(AllProvidersFailedError) as info:
        run(pair(a, b).chat(MESSAGES))
    assert len(info.value.failures) == 2
    assert "groq/a" in str(info.value)
    assert "openai/b" in str(info.value)
    assert "first-down" in str(info.value)


def test_retry_bound_is_respected_across_fallback():
    a = FakeTransport(json_responses=[HttpError(500, "x")] * 3)
    b = FakeTransport(json_responses=[OK])
    router = Router(
        {
            "groq": make("groq", a, retry=RetryPolicy(max_retries=2)),
            "openai": make("openai", b),
        },
        [ModelRef("groq", "a"), ModelRef("openai", "b")],
    )
    assert run(router.chat(MESSAGES)).message.content == "ok"
    assert len(a.calls) == 3
    assert len(b.calls) == 1


def test_retry_policy_without_jitter_keeps_exact_delays():
    policy = RetryPolicy()
    assert policy.delay(0, None) == 0.5
    assert policy.delay(1, None) == 1.0
    assert policy.delay(0, 1.5) == 1.5


def test_retry_delay_jitter_stays_within_bounds():
    low = RetryPolicy(base_delay=1.0, jitter=0.5, random_fn=lambda: 0.0)
    high = RetryPolicy(base_delay=1.0, jitter=0.5, random_fn=lambda: 1.0)
    assert low.delay(2, None) == pytest.approx(2.0)
    assert high.delay(2, None) == pytest.approx(6.0)


def test_retry_delay_jitter_is_clamped_to_max_delay():
    clamped = RetryPolicy(base_delay=8.0, max_delay=8.0, jitter=0.5, random_fn=lambda: 1.0)
    assert clamped.delay(1, None) == 8.0


def test_retry_delay_jitter_does_not_shrink_retry_after():
    policy = RetryPolicy(base_delay=1.0, jitter=0.5, random_fn=lambda: 0.0)
    assert policy.delay(0, 10.0) == 10.0


def test_invalid_jitter_rejected():
    with pytest.raises(ValueError):
        RetryPolicy(jitter=1.5)
    with pytest.raises(ValueError):
        RetryPolicy(jitter=-0.1)


def test_total_time_budget_stops_retrying():
    clock = FakeClock()
    errors = [ProviderUnavailableError("x", provider="groq", retryable=True)] * 10
    provider = ScriptedProvider(
        "groq",
        chat=errors,
        retry=RetryPolicy(max_retries=9, base_delay=0.5, max_total_seconds=1.2),
        clock=clock.time,
        sleep=clock.sleep,
    )
    router = Router({"groq": provider}, [ModelRef("groq", "a")])
    with pytest.raises(ProviderUnavailableError):
        run(router.chat(MESSAGES))
    assert provider.calls == 2
    assert clock.delays == [0.5]


def test_without_budget_all_retries_run():
    clock = FakeClock()
    errors = [ProviderUnavailableError("x", provider="groq", retryable=True)] * 5
    provider = ScriptedProvider(
        "groq",
        chat=errors,
        retry=RetryPolicy(max_retries=4, base_delay=0.1),
        clock=clock.time,
        sleep=clock.sleep,
    )
    router = Router({"groq": provider}, [ModelRef("groq", "a")])
    with pytest.raises(ProviderUnavailableError):
        run(router.chat(MESSAGES))
    assert provider.calls == 5


def test_stream_retry_respects_total_time_budget():
    clock = FakeClock()
    provider = ScriptedProvider(
        "groq",
        chat=[ProviderUnavailableError("x", provider="groq", retryable=True)] * 10,
        retry=RetryPolicy(max_retries=9, base_delay=0.5, max_total_seconds=1.2),
        clock=clock.time,
        sleep=clock.sleep,
    )

    async def go():
        async for _ in provider.stream(REQUEST):
            pass

    with pytest.raises(ProviderUnavailableError):
        run(go())
    assert provider.calls == 2


def test_empty_stream_still_detected():
    a = FakeTransport(streams=[["[DONE]"]])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    events = collect_stream(pair(a, b))
    assert events[0].text == "b-text"


def test_first_token_timeout_falls_back_and_closes_upstream():
    hanging = HangingTransport()
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    router = Router(
        {"groq": make("groq", hanging), "openai": make("openai", b)},
        [ModelRef("groq", "a"), ModelRef("openai", "b")],
        first_token_timeout=0.05,
    )
    events = collect_stream(router)
    assert events[0].text == "b-text"
    assert hanging.closed_streams == 1
    assert router.provider_cooldowns()["groq"] > 0
    records = router.attempt_log()
    assert records[0].error_category == "provider_unavailable"
    assert records[-1].first_token_ms is not None


def test_first_token_timeout_without_fallback_candidate_raises():
    hanging = HangingTransport()
    router = Router(
        {"groq": make("groq", hanging)},
        [ModelRef("groq", "a")],
        first_token_timeout=0.05,
    )
    with pytest.raises(ProviderUnavailableError, match="first token"):
        collect_stream(router)
    assert hanging.closed_streams == 1


def test_router_rejects_invalid_first_token_timeout():
    with pytest.raises(ValueError):
        Router(
            {"groq": make("groq", FakeTransport())},
            [ModelRef("groq", "a")],
            first_token_timeout=0,
        )


def test_no_silent_fallback_after_first_token_with_timeout_enabled():
    a = FakeTransport(streams=[[chunk("partial"), TransportError("boom")]])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    router = pair(a, b, first_token_timeout=5.0)
    with pytest.raises(ProviderUnavailableError):
        collect_stream(router)
    assert len(b.calls) == 0


def test_concurrent_requests_keep_router_state_consistent():
    a = FakeTransport(json_responses=[HttpError(429, "{}")] * 3 + [OK] * 5)
    b = FakeTransport(json_responses=[OK] * 8)

    async def go():
        router = pair(a, b)
        results = await asyncio.gather(
            *(router.chat(MESSAGES) for _ in range(8)), return_exceptions=True
        )
        return router, results

    router, results = run(go())
    assert all(isinstance(item, ChatResponse) for item in results)
    for remaining in router.provider_cooldowns().values():
        assert remaining > 0
    assert set(router.provider_cooldowns()) <= {"groq", "openai"}
    assert router.last_used in (ModelRef("groq", "a"), ModelRef("openai", "b"))


def test_concurrent_streams_keep_router_state_consistent():
    a = FakeTransport(streams=[HttpError(429, "{}")] * 2 + [[chunk("x"), "[DONE]"]] * 4)
    b = FakeTransport(streams=[[chunk("y"), "[DONE]"]] * 6)

    async def go():
        router = pair(a, b)

        async def consume():
            return [event async for event in router.stream(MESSAGES)]

        results = await asyncio.gather(*(consume() for _ in range(6)), return_exceptions=True)
        return router, results

    router, results = run(go())
    assert all(isinstance(item, list) and item for item in results)
    assert set(router.provider_cooldowns()) <= {"groq", "openai"}


def test_attempt_journal_records_kind_and_ttft():
    a = FakeTransport(streams=[HttpError(429, "{}")])
    b = FakeTransport(streams=[[chunk("b-text"), "[DONE]"]])
    router = pair(a, b)
    collect_stream(router)
    records = router.attempt_log()
    assert records[0].kind == "stream"
    assert records[-1].kind == "stream"
    assert records[-1].first_token_ms is not None
    assert records[-1].first_token_ms >= 0
    assert records[-1].duration_ms >= 0


def test_attempt_journal_is_bounded():
    provider = ScriptedProvider("groq")
    router = Router({"groq": provider}, [ModelRef("groq", "a")])
    for _ in range(50):
        run(router.chat(MESSAGES))
    assert len(router.attempt_log()) == 32


def test_attempt_journal_summary_has_no_request_content():
    a = FakeTransport(json_responses=[HttpError(500, "upstream exploded")])
    b = FakeTransport(json_responses=[OK])
    router = pair(a, b)
    run(router.chat(MESSAGES))
    summary = router.attempt_log()[0].error_summary
    assert "upstream exploded" in summary
    assert "hi" not in summary


def test_api_key_never_appears_in_logs_or_errors(caplog):
    transport = FakeTransport(json_responses=[HttpError(401, "nope"), OK])
    config = build_config(
        {"agent": {"model": "groq/a", "fallback": ["openai/b"]}}, {"GROQ_API_KEY": "sk-secret-123"}
    )
    router = build_router(
        config,
        transport=transport,
        environ={"GROQ_API_KEY": "sk-secret-123", "OPENAI_API_KEY": "sk-secret-123"},
    )
    with caplog.at_level(logging.WARNING, logger="eurus.providers.router"):
        assert run(router.chat(MESSAGES)).message.content == "ok"
    assert transport.calls[0].headers["Authorization"] == "Bearer sk-secret-123"
    assert "sk-secret-123" not in caplog.text
    assert all("sk-secret-123" not in record.error_summary for record in router.attempt_log())


def test_api_key_never_appears_in_all_failed_error():
    transport = FakeTransport(json_responses=[HttpError(401, "nope"), HttpError(500, "down")])
    config = build_config(
        {"agent": {"model": "groq/a", "fallback": ["openai/b"]}, "network": {"max_retries": 0}},
        {"GROQ_API_KEY": "sk-secret-123"},
    )
    router = build_router(
        config,
        transport=transport,
        environ={"GROQ_API_KEY": "sk-secret-123", "OPENAI_API_KEY": "other-secret"},
    )
    with pytest.raises(AllProvidersFailedError) as info:
        run(router.chat(MESSAGES))
    assert "sk-secret-123" not in str(info.value)
    assert "other-secret" not in str(info.value)


def config_for(tmp_path, text="", **env):
    path = tmp_path / "c.toml"
    path.write_text(text, encoding="utf-8")
    return load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path), **env})


def test_legacy_config_keeps_previous_defaults(tmp_path):
    config = config_for(tmp_path, '[agent]\nmodel = "groq/a"\n')
    assert config.network.timeout_seconds == 120.0
    assert config.network.max_retries == 3
    assert config.network.local_only is False
    assert config.network.cooldown_seconds == 30.0
    assert config.network.max_cooldown_seconds == 300.0
    assert config.network.retry_jitter == 0.0
    assert config.network.first_token_seconds is None


def test_legacy_config_builds_router_with_defaults(tmp_path):
    config = config_for(tmp_path, '[agent]\nmodel = "groq/a"\n')
    router = build_router(
        config, transport=FakeTransport(json_responses=[OK]), environ={"GROQ_API_KEY": "k"}
    )
    assert router.cooldown_seconds == 30.0
    assert router.max_cooldown_seconds == 300.0
    assert router.first_token_timeout is None
    assert router.providers["groq"].retry.jitter == 0.0
    run(router.chat(MESSAGES))


def test_new_network_keys_are_parsed(tmp_path):
    text = (
        "[network]\ncooldown_seconds = 5.0\nmax_cooldown_seconds = 60.0\n"
        "retry_jitter = 0.3\nfirst_token_seconds = 45\n"
    )
    config = config_for(tmp_path, text)
    assert config.network.cooldown_seconds == 5.0
    assert config.network.max_cooldown_seconds == 60.0
    assert config.network.retry_jitter == 0.3
    assert config.network.first_token_seconds == 45.0


def test_new_network_keys_reach_the_router(tmp_path):
    text = (
        '[agent]\nmodel = "groq/a"\n'
        "[network]\ncooldown_seconds = 5.0\nmax_cooldown_seconds = 60.0\n"
        "retry_jitter = 0.3\nfirst_token_seconds = 45\n"
    )
    config = config_for(tmp_path, text)
    router = build_router(
        config, transport=FakeTransport(json_responses=[OK]), environ={"GROQ_API_KEY": "k"}
    )
    assert router.cooldown_seconds == 5.0
    assert router.max_cooldown_seconds == 60.0
    assert router.first_token_timeout == 45.0
    assert router.providers["groq"].retry.jitter == 0.3


def test_zero_first_token_seconds_disables_timeout(tmp_path):
    config = config_for(tmp_path, "[network]\nfirst_token_seconds = 0\n")
    assert config.network.first_token_seconds is None


@pytest.mark.parametrize(
    "text",
    [
        "[network]\ncooldown_seconds = -1\n",
        "[network]\ncooldown_seconds = 4000\n",
        "[network]\nmax_cooldown_seconds = -5\n",
        "[network]\ncooldown_seconds = 300\nmax_cooldown_seconds = 60\n",
        "[network]\nretry_jitter = 1.5\n",
        "[network]\nretry_jitter = -0.2\n",
        "[network]\nfirst_token_seconds = 0.5\n",
        "[network]\nfirst_token_seconds = 601\n",
        "[network]\nfirst_token_seconds = 'soon'\n",
        "[network]\nunknown_key = 1\n",
    ],
)
def test_invalid_new_network_values_rejected(tmp_path, text):
    with pytest.raises(Exception) as info:
        config_for(tmp_path, text)
    assert type(info.value).__name__ in {"ConfigError", "tomllib.TOMLDecodeError"}


def test_new_network_env_overrides(tmp_path):
    config = config_for(
        tmp_path,
        EURUS_COOLDOWN="7",
        EURUS_MAX_COOLDOWN="90",
        EURUS_RETRY_JITTER="0.25",
        EURUS_FIRST_TOKEN="30",
    )
    assert config.network.cooldown_seconds == 7.0
    assert config.network.max_cooldown_seconds == 90.0
    assert config.network.retry_jitter == 0.25
    assert config.network.first_token_seconds == 30.0


def test_sample_config_still_loads(tmp_path):
    from eurus.config import sample_config

    path = tmp_path / "c.toml"
    path.write_text(sample_config({"TERMUX_VERSION": ""}), encoding="utf-8")
    config = load_config(config_path=path, environ={"EURUS_HOME": str(tmp_path)})
    assert config.network.cooldown_seconds == 30.0
    assert config.network.max_cooldown_seconds == 300.0
    assert config.network.retry_jitter == 0.0
    assert config.network.first_token_seconds is None


def test_jittered_backoff_through_provider_retry():
    a = FakeTransport(json_responses=[HttpError(500, "x"), OK])
    provider = make("groq", a, retry=RetryPolicy(max_retries=1, jitter=0.5, random_fn=lambda: 1.0))
    response = run(provider.chat(REQUEST))
    assert response.message.content == "ok"
    assert len(a.calls) == 2
