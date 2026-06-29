"""Orquestração: itera fases por story, roda retrospective ao fim da epic,
aplica regras de git e emite eventos. Respeita pause/stop e checkpoints
humanos (via RunControl)."""

from __future__ import annotations

import asyncio

from . import events as ev
from . import router
from .advisor import Advisor
from .config import RETROSPECTIVE, Config
from .events import EventSink, RunControl, StopRequested
from .git_rules import GitContext, GitRunner, apply_phase
from .status import SprintStatus, parse_story_key
from .worker import run_phase


async def _checkpoint(label: str, cfg: Config, sink: EventSink, control: RunControl) -> None:
    await sink.emit(ev.checkpoint_hit(label))
    if control.interactive_cli:
        # modo CLI puro: bloqueia no terminal
        try:
            input(f"\n⏸  checkpoint: {label} — Enter para continuar (Ctrl-C aborta)… ")
        except EOFError:
            pass
    else:
        # modo app/servidor: aguarda comando approve (stop também libera)
        await control.wait_approval()


async def _run_one_phase(
    skill: str, target_id: str, cfg: Config, advisor_cls,
    sink: EventSink, control: RunControl, dry_run: bool,
    done_predicate=None,
) -> None:
    if dry_run:
        # Pura simulação: não abre sessão Claude (advisor) nenhuma.
        await run_phase(skill, target_id, cfg, None, sink, control, dry_run=True)
        return
    # Uma sessão advisor por fase (contexto próprio).
    async with advisor_cls(cfg, sink) as advisor:
        await run_phase(skill, target_id, cfg, advisor, sink, control,
                        dry_run=False, done_predicate=done_predicate)


async def process_story(
    story_key: str, cfg: Config, status: SprintStatus, runner: GitRunner,
    sink: EventSink, control: RunControl, dry_run: bool,
) -> None:
    parsed = parse_story_key(story_key)
    epic_id = str(parsed.epic) if parsed else ""

    if dry_run:
        # Simula o ciclo COMPLETO da story (sem executar nem persistir).
        cur = status.story_status(story_key) or router.BACKLOG
        for phase in router.lifecycle(cur):
            control.raise_if_stopped()
            await _run_one_phase(phase.skill, story_key, cfg, Advisor, sink, control, True)
            await sink.emit(ev.status_changed(story_key, phase.next_status))
            ctx = GitContext(story_id=story_key, epic_id=epic_id)
            await apply_phase(cfg.phase(phase.skill), runner, ctx, sink)
        return

    while (phase := status.next_phase(story_key)) is not None:
        control.raise_if_stopped()
        target = phase.next_status
        done_pred = lambda t=target: status.story_status(story_key) == t
        await _run_one_phase(phase.skill, story_key, cfg, Advisor, sink, control,
                             dry_run, done_pred)

        frm = status.story_status(story_key)
        status.set_status(story_key, phase.next_status)
        await sink.emit(ev.status_changed(story_key, phase.next_status, frm))

        ctx = GitContext(story_id=story_key, epic_id=epic_id)
        await apply_phase(cfg.phase(phase.skill), runner, ctx, sink)

        if cfg.autonomy.human_checkpoint == "end-of-story" and phase.next_status == "done":
            await _checkpoint(f"story {story_key} concluída", cfg, sink, control)


async def run_epic(
    epic_id: str, cfg: Config, status: SprintStatus, runner: GitRunner,
    sink: EventSink, control: RunControl, dry_run: bool,
) -> None:
    stories = status.epic_stories(epic_id)
    if not stories:
        raise SystemExit(f"nenhuma story encontrada para a epic {epic_id}")
    await sink.emit(ev.log(f"epic {epic_id}: {len(stories)} stories"))
    for s in stories:
        control.raise_if_stopped()
        await process_story(s.key, cfg, status, runner, sink, control, dry_run)

    if dry_run:
        await sink.emit(ev.log(f"[dry-run] ao completar a epic rodaria {RETROSPECTIVE}"))
        return
    if status.epic_complete(epic_id):
        if cfg.autonomy.human_checkpoint == "retrospective":
            await _checkpoint(f"epic {epic_id} concluída", cfg, sink, control)
        retro_key = status.retrospective_key(epic_id)
        retro_done = lambda: status.story_status(retro_key) == "done"
        await _run_one_phase(RETROSPECTIVE, str(epic_id), cfg, Advisor, sink, control,
                             dry_run, retro_done)
        try:
            status.set_status(retro_key, "done")
            await sink.emit(ev.status_changed(retro_key, "done"))
        except KeyError:
            await sink.emit(ev.log(f"'{retro_key}' não existe no sprint-status; pulando", "warn"))
        ctx = GitContext(story_id="", epic_id=str(epic_id))
        await apply_phase(cfg.phase(RETROSPECTIVE), runner, ctx, sink)
    else:
        pending = [s.key for s in stories if status.story_status(s.key) != "done"]
        await sink.emit(ev.log(f"epic {epic_id} incompleta; pendentes: {pending}", "warn"))


async def run(
    cfg: Config, *, story: str | None, epic: str | None, dry_run: bool,
    sink: EventSink, control: RunControl,
) -> None:
    scope = "story" if story else "epic"
    target = story or epic or ""
    await sink.emit(ev.run_started(scope, target, dry_run))
    status = SprintStatus(cfg.sprint_status_file)
    runner = GitRunner(cfg.bmad_project_dir, dry_run=dry_run)
    try:
        if story:
            await process_story(story, cfg, status, runner, sink, control, dry_run)
        elif epic:
            await run_epic(epic, cfg, status, runner, sink, control, dry_run)
        else:
            raise SystemExit("informe --story ou --epic")
        await sink.emit(ev.run_ended(True))
    except StopRequested:
        await sink.emit(ev.run_ended(False, "parado pelo usuário"))
    except asyncio.CancelledError:
        await sink.emit(ev.run_ended(False, "parado pelo usuário"))
        raise
    except Exception as exc:  # noqa: BLE001 — reporta e encerra o run limpo
        await sink.emit(ev.error(str(exc)))
        await sink.emit(ev.run_ended(False, str(exc)))
        raise
