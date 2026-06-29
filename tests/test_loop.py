"""Testes do loop em dry-run (sem chamar Claude) e das regras de git."""

import asyncio
import subprocess
from pathlib import Path

from autopilot.config import config_for_project, default_phases
from autopilot.events import EventSink, RunControl
from autopilot.git_rules import GitContext, GitRunner, apply_phase
from autopilot.loop import run as run_loop


def _collect(coro):
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))
    asyncio.run(coro(sink))
    return kinds


def test_dry_run_story_full_cycle(fixture_project: Path):
    cfg = config_for_project(fixture_project, phases=default_phases())

    async def go(sink):
        await run_loop(cfg, story="7-2-create-api", epic=None, dry_run=True,
                       sink=sink, control=RunControl())

    kinds = _collect(go)
    # 7-2 está ready-for-dev -> dev-story, code-review (ciclo completo simulado)
    assert kinds[0] == "run_started"
    assert kinds[-1] == "run_ended"
    assert kinds.count("phase_started") == 2
    assert "status_changed" in kinds
    assert "git_action" in kinds


def test_git_rules_dry_run_emits_events(fixture_project: Path):
    cfg = config_for_project(fixture_project, phases=default_phases())
    sink = EventSink()
    ops: list[str] = []
    sink.add_callback(lambda e: ops.append(e.data.get("op")) if e.kind == "git_action" else None)
    runner = GitRunner(fixture_project, dry_run=True)
    ctx = GitContext(story_id="7-2-create-api", epic_id="7")

    asyncio.run(apply_phase(cfg.phase("bmad-code-review"), runner, ctx, sink))
    assert ops == ["commit", "open_pr", "merge_pr"]


def test_merge_pr_syncs_local_base():
    """Após o merge (no remoto), o base LOCAL precisa ser sincronizado — senão o
    orquestrador relê um sprint-status defasado e re-roda a story em loop."""
    calls: list[list[str]] = []

    class Rec(GitRunner):
        def run(self, args, *, check=True):
            calls.append(list(args))
            return subprocess.CompletedProcess(args, 0, "", "")

    r = Rec(Path("."), dry_run=False)
    ctx = GitContext(story_id="7-2-create-api", branch="story/7-2-create-api", base="main")
    r.merge_pr("squash", ctx)

    assert ["gh", "pr", "merge", "story/7-2-create-api", "--squash", "--delete-branch"] in calls
    # sincroniza o main local com o remoto pós-merge
    assert ["git", "checkout", "main"] in calls
    assert ["git", "fetch", "origin", "main"] in calls
    assert ["git", "reset", "--hard", "origin/main"] in calls
