from __future__ import annotations

import asyncio

from eurus.research.models import ResearchMode
from eurus.research.planning import (
    QueryPlanner,
    deterministic_plan,
    normalize_query_text,
)
from eurus.research.settings import ResearchSettings


class FakeRouter:
    def __init__(self, content: str | None = None, error: BaseException | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    async def chat(self, messages, *, tools=(), temperature=None, max_tokens=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        message = type("M", (), {"content": self.content})()
        return type("R", (), {"message": message})()


def test_deterministic_plan_plain_query() -> None:
    plan = deterministic_plan(
        "solid state battery progress",
        ResearchMode.STANDARD,
        budget_queries=3,
        max_supporting=2,
    )
    assert plan.main_query == "solid state battery progress"
    assert plan.planner == "deterministic"
    assert plan.queries[0].purpose == "main"
    assert len(plan.queries) <= 3
    assert plan.search_budget == len(plan.queries)


def test_deterministic_plan_comparison_decomposes() -> None:
    plan = deterministic_plan(
        "cats vs dogs as pets",
        ResearchMode.DEEP,
        budget_queries=6,
        max_supporting=3,
    )
    texts = [query.text for query in plan.queries]
    assert any("cats" in text for text in texts)
    assert any("dogs" in text for text in texts)
    assert plan.subquestions and any("cats" in item for item in plan.subquestions)
    assert any("dogs" in item for item in plan.subquestions)


def test_deterministic_plan_question_strips_question_words() -> None:
    plan = deterministic_plan(
        "how does photosynthesis work",
        ResearchMode.STANDARD,
        budget_queries=4,
        max_supporting=2,
    )
    assert plan.subquestions
    assert all("photosynthesis" in item for item in plan.subquestions)


def test_deterministic_plan_detects_time_range() -> None:
    plan = deterministic_plan(
        "battery technology 2024-2026 overview",
        ResearchMode.DEEP,
        budget_queries=5,
        max_supporting=2,
    )
    assert plan.time_constraint == "2024 to 2026"


def test_deterministic_plan_eliminates_duplicates() -> None:
    plan = deterministic_plan(
        "cats vs cats",
        ResearchMode.STANDARD,
        budget_queries=4,
        max_supporting=3,
    )
    keys = [normalize_query_text(query.text) for query in plan.queries]
    assert len(keys) == len(set(keys))


def test_deterministic_plan_respects_budget() -> None:
    plan = deterministic_plan(
        "a vs b comparison of frameworks",
        ResearchMode.DEEP,
        budget_queries=2,
        max_supporting=5,
    )
    assert len(plan.queries) <= 2


def test_followups_generate_for_gaps() -> None:
    planner = QueryPlanner(settings=ResearchSettings())
    plan = deterministic_plan(
        "solid state battery",
        ResearchMode.DEEP,
        budget_queries=12,
        max_supporting=2,
    )
    followups = planner.followups(
        plan,
        ["energy density of solid state batteries"],
        ["solid state battery"],
        budget_queries=3,
        round_hint=2,
    )
    assert followups
    assert all(query.purpose == "followup" for query in followups)
    assert all(query.round_hint == 2 for query in followups)
    keys = {normalize_query_text(query.text) for query in followups}
    assert "solid state battery" not in keys


def test_followups_respect_used_queries() -> None:
    planner = QueryPlanner(settings=ResearchSettings())
    plan = deterministic_plan(
        "battery",
        ResearchMode.DEEP,
        budget_queries=10,
        max_supporting=1,
    )
    used = ["battery energy density trends 2026"]
    followups = planner.followups(
        plan,
        ["energy density"],
        used,
        budget_queries=3,
        round_hint=2,
    )
    for query in followups:
        assert normalize_query_text(query.text) not in {normalize_query_text(u) for u in used}


async def run_plan_with_model(router: FakeRouter, objective: str = "compare A vs B"):
    planner = QueryPlanner(settings=ResearchSettings(), router=router)
    return await planner.plan_with_model(
        objective,
        ResearchMode.DEEP,
        budget_queries=8,
        max_supporting=4,
    )


def test_model_plan_accepted_when_valid() -> None:
    router = FakeRouter(
        content=(
            '{"objective": "compare A vs B", "subquestions": ["what is A", "what is B"], '
            '"queries": [{"text": "A overview", "purpose": "main"}, '
            '{"text": "A overview"}, {"text": "B overview"}], '
            '"time_constraint": "", "source_types": ["reference"], "depth": "deep"}'
        )
    )
    plan = asyncio.run(run_plan_with_model(router))
    assert plan.planner == "model"
    assert plan.main_query == "A overview"
    assert router.calls == 1
    assert len(plan.subquestions) == 2


def test_model_plan_falls_back_on_invalid_json() -> None:
    router = FakeRouter(content="this is not json")
    plan = asyncio.run(run_plan_with_model(router))
    assert plan.planner == "deterministic"


def test_model_plan_falls_back_on_invalid_structure() -> None:
    router = FakeRouter(content='{"subquestions": "must be a list not a string"}')
    plan = asyncio.run(run_plan_with_model(router))
    assert plan.planner == "deterministic"


def test_model_plan_falls_back_on_error() -> None:
    router = FakeRouter(error=TimeoutError("model timed out"))
    plan = asyncio.run(run_plan_with_model(router))
    assert plan.planner == "deterministic"


def test_model_plan_not_used_for_standard_mode() -> None:
    router = FakeRouter(content='{"objective": "x", "queries": [{"text": "y"}]}')
    planner = QueryPlanner(settings=ResearchSettings(), router=router)
    plan = asyncio.run(
        planner.plan_with_model(
            "objective",
            ResearchMode.STANDARD,
            budget_queries=5,
            max_supporting=2,
        )
    )
    assert plan.planner == "deterministic"
    assert router.calls == 0


def test_model_plan_dedupes_and_caps_queries() -> None:
    router = FakeRouter(
        content=(
            '{"objective": "x", "queries": ['
            '{"text": "same query", "purpose": "main"},'
            '{"text": "same query"},'
            '{"text": "another", "purpose": "weird"},'
            '{"text": "", "purpose": "main"}'
            "]}"
        )
    )
    plan = asyncio.run(run_plan_with_model(router))
    keys = [normalize_query_text(query.text) for query in plan.queries]
    assert len(keys) == len(set(keys))
    assert all(key for key in keys)
