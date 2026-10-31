from __future__ import annotations

import asyncio
import json
import time

import click

from eurus import paths
from eurus.cli.base import _state, main
from eurus.errors import EurusError
from eurus.evals import TASKS, EvalResult, run_task
from eurus.evals.results import EvaluationRecord, EvaluationStore
from eurus.providers.factory import build_router
from eurus.sessions import SessionStore


def _compare_learning_report(
    baseline: list[EvalResult], treated: list[EvalResult]
) -> dict[str, object]:
    baseline_by_task = {item.task: item for item in baseline}
    treated_by_task = {item.task: item for item in treated}
    regressions: list[str] = []
    improvements: list[str] = []
    for task_name, treated_item in treated_by_task.items():
        baseline_item = baseline_by_task.get(task_name)
        if baseline_item is None:
            continue
        if baseline_item.passed and not treated_item.passed:
            regressions.append(task_name)
        if not baseline_item.passed and treated_item.passed:
            improvements.append(task_name)
    baseline_passed = sum(item.passed for item in baseline)
    treated_passed = sum(item.passed for item in treated)
    return {
        "schema_version": 1,
        "comparison": "lessons_off_vs_lessons_on",
        "baseline": {
            "passed": baseline_passed,
            "total": len(baseline),
            "pass_rate": baseline_passed / len(baseline) if baseline else None,
            "mean_steps": sum(item.steps for item in baseline) / len(baseline)
            if baseline
            else None,
            "mean_tokens": sum(item.tokens for item in baseline) / len(baseline)
            if baseline
            else None,
        },
        "treated": {
            "passed": treated_passed,
            "total": len(treated),
            "pass_rate": treated_passed / len(treated) if treated else None,
            "mean_steps": sum(item.steps for item in treated) / len(treated) if treated else None,
            "mean_tokens": sum(item.tokens for item in treated) / len(treated) if treated else None,
        },
        "regressions": regressions,
        "improvements": improvements,
        "learning_helps": (
            treated_passed > baseline_passed and not regressions if baseline and treated else None
        ),
    }


def _record_suite(
    store: EvaluationStore, revision: str, results: list[EvalResult], *, lessons: bool
) -> None:
    for item in results:
        store.record(
            EvaluationRecord(
                suite="learning-compare",
                revision=revision,
                task=item.task,
                passed=item.passed,
                steps=item.steps,
                tokens=item.tokens,
                seconds=item.seconds,
                stop_reason=item.stop_reason,
                detail=item.detail,
                metrics={"lessons": lessons},
            )
        )


@main.command("eval")
@click.option("--task", "names", multiple=True, help="Task name to run; repeat for several.")
@click.option("--lessons/--no-lessons", default=False, help="Inject saved lessons into prompts.")
@click.option(
    "--compare-learning",
    is_flag=True,
    help="Run the suite with lessons off and on, then report the learning delta.",
)
@click.pass_context
def eval_command(
    ctx: click.Context, names: tuple[str, ...], lessons: bool, compare_learning: bool
) -> None:

    config = _state(ctx).config
    known = {task.name for task in TASKS}
    unknown = sorted(set(names) - known)
    if unknown:
        raise click.ClickException(f"unknown task: {', '.join(unknown)}")
    selected = [task for task in TASKS if not names or task.name in names]

    if compare_learning:

        async def run_suite(with_lessons: bool) -> list[EvalResult]:
            router = build_router(config)
            try:
                return [
                    await run_task(task, config, router, lessons=with_lessons) for task in selected
                ]
            except EurusError as exc:
                raise click.ClickException(str(exc)) from exc
            finally:
                await router.aclose()

        baseline = asyncio.run(run_suite(False))
        treated = asyncio.run(run_suite(True))
        stamp = time.strftime("%Y%m%d-%H%M%S")
        store = EvaluationStore(paths.home_manager().evals_dir / "learning-evals.sqlite3")
        _record_suite(store, f"lessons-off-{stamp}", baseline, lessons=False)
        _record_suite(store, f"lessons-on-{stamp}", treated, lessons=True)
        report = _compare_learning_report(baseline, treated)
        report["model"] = config.agent.model
        report["baseline_results"] = [item.to_dict() for item in baseline]
        report["treated_results"] = [item.to_dict() for item in treated]
        target = paths.home_dir() / "evals"
        target.mkdir(parents=True, exist_ok=True)
        file = target / f"learning-compare-{stamp}.json"
        file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        click.echo(json.dumps({key: report[key] for key in ("baseline", "treated")}, indent=2))
        regressions = report["regressions"]
        improvements = report["improvements"]
        if isinstance(regressions, list) and regressions:
            click.echo(f"REGRESSIONS with lessons enabled: {', '.join(regressions)}")
        if isinstance(improvements, list) and improvements:
            click.echo(f"improvements with lessons enabled: {', '.join(improvements)}")
        click.echo(f"learning_helps={report['learning_helps']}")
        click.echo(f"saved {file}", err=True)
        return

    async def go() -> list[EvalResult]:
        router = build_router(config)
        try:
            return [await run_task(task, config, router, lessons=lessons) for task in selected]
        except EurusError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            await router.aclose()

    results = asyncio.run(go())
    for item in results:
        mark = "pass" if item.passed else "FAIL"
        click.echo(
            f"{mark}  {item.task:<22} steps={item.steps:<3} tokens={item.tokens:<7} "
            f"{item.seconds:.1f}s {item.stop_reason}"
        )
    passed = sum(item.passed for item in results)
    click.echo(f"{passed}/{len(results)} passed (lessons={'on' if lessons else 'off'})")
    target = paths.home_dir() / "evals"
    target.mkdir(parents=True, exist_ok=True)
    report = {
        "model": config.agent.model,
        "lessons": lessons,
        "metrics": {
            "task_count": len(results),
            "passed": passed,
            "pass_rate": passed / len(results) if results else 0.0,
            "average_steps": (
                sum(item.steps for item in results) / len(results) if results else 0.0
            ),
            "average_tokens": (
                sum(item.tokens for item in results) / len(results) if results else 0.0
            ),
            "average_seconds": (
                sum(item.seconds for item in results) / len(results) if results else 0.0
            ),
        },
        "results": [item.to_dict() for item in results],
    }
    file = target / f"{time.strftime('%Y%m%d-%H%M%S')}.json"
    file.write_text(json.dumps(report, indent=2), encoding="utf-8")
    click.echo(f"saved {file}", err=True)


@main.command("benchmark")
@click.option("--task", "names", multiple=True, help="Task name to run; repeat for several.")
@click.option("--lessons/--no-lessons", default=False, help="Inject saved lessons into prompts.")
@click.pass_context
def benchmark_command(ctx: click.Context, names: tuple[str, ...], lessons: bool) -> None:
    config = _state(ctx).config
    known = {task.name for task in TASKS}
    unknown = sorted(set(names) - known)
    if unknown:
        raise click.ClickException(f"unknown task: {', '.join(unknown)}")
    selected = [task for task in TASKS if not names or task.name in names]

    async def go() -> list[EvalResult]:
        router = build_router(config)
        try:
            return [await run_task(task, config, router, lessons=lessons) for task in selected]
        except EurusError as exc:
            raise click.ClickException(str(exc)) from exc
        finally:
            await router.aclose()

    results = asyncio.run(go())
    passed = sum(item.passed for item in results)
    report = {
        "schema_version": 1,
        "benchmark_kind": "runtime_task_fixtures",
        "model": config.agent.model,
        "lessons": lessons,
        "metrics": {
            "task_count": len(results),
            "passed": passed,
            "task_success_rate": passed / len(results) if results else None,
            "mean_latency_seconds": (
                sum(item.seconds for item in results) / len(results) if results else None
            ),
            "mean_steps": (sum(item.steps for item in results) / len(results) if results else None),
            "mean_tokens": (
                sum(item.tokens for item in results) / len(results) if results else None
            ),
            "recovery_success_rate": None,
            "duplicate_side_effect_rate": None,
            "provider_cost": None,
        },
        "unavailable_metrics": {
            "recovery_success_rate": "This benchmark does not inject runtime faults.",
            "duplicate_side_effect_rate": "The fixtures do not instrument external side effects.",
            "provider_cost": "The provider usage contract does not expose cost.",
        },
        "results": [item.to_dict() for item in results],
    }
    target = paths.home_dir() / "evals"
    target.mkdir(parents=True, exist_ok=True)
    file = target / f"benchmark-{time.time_ns()}.json"
    file.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    click.echo(json.dumps(report["metrics"], indent=2, ensure_ascii=False))
    click.echo(f"saved {file}", err=True)
    if passed != len(results):
        raise click.ClickException(f"benchmark failures: {len(results) - passed}/{len(results)}")


@main.command("metrics")
@click.option("--days", type=click.IntRange(1, 3650), default=30, show_default=True)
def metrics_command(days: int) -> None:

    store = SessionStore(paths.database_path())
    metrics = store.event_metrics(days=days)
    rates = store.event_rates(days=days)
    click.echo(f"Lifecycle events in the last {days} days:")
    if not metrics:
        click.echo("No lifecycle events recorded in this window.")
    else:
        for event_type, count in metrics.items():
            click.echo(f"{event_type}: {count}")
    click.echo(f"Approval decisions: {rates['approval_decisions']}")
    click.echo(f"Approval rejection rate: {rates['approval_rejection_rate']:.1%}")
    click.echo(f"Correction proposals: {rates['correction_proposals']}")
    click.echo(f"Correction decisions: {rates['correction_decisions']}")
    click.echo(f"Correction acceptance rate: {rates['correction_acceptance_rate']:.1%}")
    click.echo(f"Correction rejection rate: {rates['correction_rejection_rate']:.1%}")
