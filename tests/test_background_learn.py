"""Phase 1.3: post-answer learning moves off the turn's critical path."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from eurus.agent.events import AgentEvent
from eurus.agent.factory import build_agent
from eurus.agent.loop import Agent
from eurus.config import Config
from eurus.providers.testing import openai_stream, scripted_router
from eurus.tools.base import ToolContext, ToolRegistry


class SlowLearner:
    def __init__(self, delay: float = 0.3) -> None:
        self.delay = delay
        self.calls = 0

    async def learn(self, task, transcript, approver):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return ["slow lesson"]


class Recorder:
    def __init__(self) -> None:
        self.events: list[AgentEvent] = []

    def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)


def make_agent(router, learner=None, **kwargs) -> Agent:
    return Agent(
        router,
        ToolRegistry([]),
        ToolContext(workspace=Path(".")),
        system_prompt="sys",
        learner=learner,
        **kwargs,
    )


def test_background_learn_does_not_delay_turn_end():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    learner = SlowLearner(delay=0.3)
    agent = make_agent(router, learner=learner, learn_in_background=True)

    async def go():
        started = time.monotonic()
        events = [event async for event in agent.run("task")]
        elapsed = time.monotonic() - started
        assert events[-1].kind == "done"
        assert elapsed < 0.25, "turn must end before the slow learner finishes"
        await agent.drain_background()
        assert agent._background_tasks == set()

    asyncio.run(go())
    assert learner.calls == 1


def test_background_learn_sink_receives_lesson_events():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    learner = SlowLearner(delay=0.05)
    recorder = Recorder()
    agent = make_agent(
        router, learner=learner, learn_in_background=True, background_event_sink=recorder
    )

    async def go():
        async for _ in agent.run("task"):
            pass
        assert recorder.events == []
        await agent.drain_background()
        assert [event.kind for event in recorder.events] == ["lesson"]
        assert recorder.events[0].text == "slow lesson"

    asyncio.run(go())


def test_inline_learn_default_still_yields_after_done():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    learner = SlowLearner(delay=0.01)
    agent = make_agent(router, learner=learner, learn_in_background=False)

    async def go():
        return [event async for event in agent.run("task")]

    events = asyncio.run(go())
    assert events[-1].kind == "lesson" and events[-1].text == "slow lesson"


def test_drain_background_cancels_on_timeout():
    router, _ = scripted_router(streams=[openai_stream("ok")])
    learner = SlowLearner(delay=5.0)
    agent = make_agent(router, learner=learner, learn_in_background=True)

    async def go():
        async for _ in agent.run("task"):
            pass
        started = time.monotonic()
        await agent.drain_background(timeout=0.1)
        assert time.monotonic() - started < 1.0
        assert agent._background_tasks == set()

    asyncio.run(go())


def test_background_learner_exception_is_contained():
    class ExplodingLearner:
        async def learn(self, task, transcript, approver):
            raise RuntimeError("boom")

    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = make_agent(router, learner=ExplodingLearner(), learn_in_background=True)

    async def go():
        async for _ in agent.run("task"):
            pass
        await agent.drain_background()

    asyncio.run(go())  # must not raise


def test_factory_mode_off_disables_reflector_and_skill_learner(tmp_path):
    config = Config()
    config = type(config)(
        **{
            **{name: getattr(config, name) for name in config.__dataclass_fields__},
            "learning": type(config.learning)(mode="off", skills=True),
            "permissions": type(config.permissions)(workspace=str(tmp_path)),
        }
    )
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = build_agent(config, router, workspace=tmp_path, learn=True)
    assert agent.learner is None
    assert agent.skill_learner is None


def test_factory_tui_default_keeps_skill_learner_off(tmp_path):
    config = Config()
    config = type(config)(
        **{
            **{name: getattr(config, name) for name in config.__dataclass_fields__},
            "permissions": type(config.permissions)(workspace=str(tmp_path)),
        }
    )
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = build_agent(
        config, router, workspace=tmp_path, learn=True, learn_background=True, skill_learning=False
    )
    assert agent.learner is not None, "reflector stays on for mode=ask"
    assert agent.skill_learner is None

    enabled = build_agent(config, router, workspace=tmp_path, learn=True, skill_learning=True)
    assert enabled.skill_learner is not None


def test_factory_auto_keeps_legacy_behavior_without_explicit_flag(tmp_path):
    config = Config()
    config = type(config)(
        **{
            **{name: getattr(config, name) for name in config.__dataclass_fields__},
            "permissions": type(config.permissions)(workspace=str(tmp_path)),
        }
    )
    router, _ = scripted_router(streams=[openai_stream("ok")])
    agent = build_agent(config, router, workspace=tmp_path, learn=True)
    assert agent.skill_learner is not None
