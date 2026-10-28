from __future__ import annotations

from pathlib import Path

import click

from eurus.cli.base import main
from eurus.learning.skills import SkillDraftStore
from eurus.learning.validation import (
    create_candidate,
    promote_candidate,
    validate_candidate,
)


@main.group("skills")
def skills_group() -> None:
    pass


@skills_group.command("list")
@click.option(
    "--quarantine/--no-quarantine",
    default=True,
    show_default=True,
    help="List quarantined skill drafts.",
)
def skills_list(quarantine: bool) -> None:
    if not quarantine:
        raise click.ClickException("only quarantined drafts are listed by this command")
    store = SkillDraftStore()
    if not store.root.exists():
        click.echo("No quarantined skill drafts.")
        return
    drafts = sorted(store.root.glob("*.SKILL.md"))
    if not drafts:
        click.echo("No quarantined skill drafts.")
        return
    for path in drafts:
        name = path.name[: -len(".SKILL.md")]
        try:
            draft = store.read(name)
            findings = ", ".join(draft.findings) if draft.findings else "clean"
            click.echo(f"{name:<24} sha={draft.sha256[:12]}  scan={findings}")
        except (ValueError, OSError):
            click.echo(f"{name:<24} unreadable draft")


@skills_group.command("show")
@click.argument("name")
def skills_show(name: str) -> None:
    store = SkillDraftStore()
    try:
        draft = store.read(name)
    except (ValueError, OSError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(draft.content)


@skills_group.command("validate")
@click.argument("name")
@click.option(
    "--check",
    "checks",
    multiple=True,
    required=True,
    help="Validation check that passed for this skill; repeat for several.",
)
@click.option(
    "--outcome",
    "outcomes",
    multiple=True,
    required=True,
    help="Expected observable outcome of applying this skill; repeat for several.",
)
def skills_validate(name: str, checks: tuple[str, ...], outcomes: tuple[str, ...]) -> None:
    store = SkillDraftStore()
    try:
        draft = store.read(name)
        candidate = create_candidate(
            name=draft.name,
            description=draft.description,
            procedure=draft.content,
            source_task_id=f"quarantine:{draft.sha256[:16]}",
            evidence=(f"quarantine draft {draft.path}",),
            expected_outcomes=outcomes,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    validated = validate_candidate(candidate, checks=checks)
    if validated.validated:
        click.echo(f"skill {name} passed validation (sha={validated.sha256[:12]})")
        return
    for note in validated.validation_notes:
        click.echo(f"- {note}")
    raise click.ClickException("validation failed; address the findings above")


@skills_group.command("promote")
@click.argument("name")
@click.option("--yes", is_flag=True, help="Confirm owner approval of this skill.")
@click.option(
    "--workspace",
    default=".",
    show_default=True,
    help="Workspace that will receive the promoted skill.",
)
@click.option(
    "--outcome",
    "outcomes",
    multiple=True,
    required=True,
    help="Expected observable outcome of applying this skill; repeat for several.",
)
def skills_promote(name: str, yes: bool, workspace: str, outcomes: tuple[str, ...]) -> None:
    if not yes:
        raise click.ClickException("owner approval is required; rerun with --yes")
    store = SkillDraftStore()
    target = Path(workspace).expanduser().resolve()
    try:
        draft = store.read(name)
        candidate = create_candidate(
            name=draft.name,
            description=draft.description,
            procedure=draft.content,
            source_task_id=f"quarantine:{draft.sha256[:16]}",
            evidence=(f"quarantine draft {draft.path}",),
            expected_outcomes=outcomes,
        )
        validated = validate_candidate(
            candidate, checks=("quarantine safety scan", "manual content review")
        )
        destination = promote_candidate(validated, store, target, approved=True)
    except (ValueError, PermissionError) as exc:
        raise click.ClickException(str(exc)) from exc
    except OSError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"promoted {name} -> {destination}")


@skills_group.command("patch")
@click.argument("name")
@click.option(
    "--description",
    default=None,
    help="Replacement description; the existing one is kept when omitted.",
)
@click.option(
    "--content-file",
    required=True,
    type=click.Path(exists=True, dir_okay=False, readable=True),
    help="File with the replacement skill content.",
)
@click.option(
    "--workspace",
    default=".",
    show_default=True,
    help="Workspace containing the skill.",
)
def skills_patch(name: str, description: str | None, content_file: str, workspace: str) -> None:
    store = SkillDraftStore()
    target = Path(workspace).expanduser().resolve()
    skill_root = target / ".eurus" / "skills" / name / "SKILL.md"
    if not skill_root.exists():
        raise click.ClickException(f"skill {name} is not installed in this workspace")
    try:
        existing = store.read(name) if store.root.exists() else None
    except (ValueError, OSError):
        existing = None
    replacement_description = description or (existing.description if existing else "")
    if not replacement_description.strip():
        raise click.ClickException("a description is required; pass --description")
    content = Path(content_file).expanduser().read_text(encoding="utf-8")
    try:
        destination = store.patch(name, target, replacement_description, content)
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"patched {name} -> {destination} (previous version backed up)")
