"""Orquestração: itera fases por story, roda retrospective ao fim da epic,
aplica regras de git e emite eventos. Respeita pause/stop e checkpoints
humanos (via RunControl)."""

from __future__ import annotations

import asyncio

from . import events as ev
from . import router
from .advisor import Advisor
from .config import CORRECT_COURSE, RETROSPECTIVE, Config
from .events import Escalation, EventSink, RunControl, StopRequested
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
) -> Escalation | None:
    """Roda uma fase. Devolve a escalação que o advisor pediu (se houver) — o
    canal pelo qual o loop fica sabendo que uma skill de recuperação é necessária."""
    if dry_run:
        # Pura simulação: não abre sessão Claude (advisor) nenhuma.
        await run_phase(skill, target_id, cfg, None, sink, control, dry_run=True)
        return None
    # Uma sessão advisor por fase (contexto próprio).
    async with advisor_cls(cfg, sink) as advisor:
        await run_phase(skill, target_id, cfg, advisor, sink, control,
                        dry_run=False, done_predicate=done_predicate)
        return advisor.last_escalation


async def _ask_recovery(esc: Escalation, sink: EventSink, control: RunControl) -> bool:
    """Surface a escalação ao humano e devolve True se ele aprovar rodar."""
    await sink.emit(ev.recovery_recommended(esc.skill, esc.reason))
    if control.interactive_cli:
        try:
            ans = input(
                f"\n⚠ recuperação recomendada: {esc.skill} — {esc.reason}\n"
                "  rodar agora? [y/N] ")
        except EOFError:
            ans = ""
        return ans.strip().lower().startswith("y")
    return (await control.wait_recovery_choice()) == "run"


async def _handle_recovery(
    esc: Escalation, story_key: str, epic_id: str, cfg: Config,
    runner: GitRunner, sink: EventSink, control: RunControl,
) -> bool:
    """Aplica a política de recuperação à escalação do advisor.

    Política `tiered` (default): quick-dev roda autônomo; correct-course pausa p/
    aprovação humana. `pause`: ambos pausam. `auto`: ambos rodam.
    Retorna True se a skill de recuperação RODOU (caller deve re-avaliar o status),
    False se foi pulada (caller segue o fluxo normal da fase)."""
    policy = cfg.autonomy.recovery_policy
    is_plan = esc.skill == CORRECT_COURSE
    if policy == "auto":
        run_it = True
    elif policy == "pause":
        run_it = await _ask_recovery(esc, sink, control)
    else:  # tiered
        run_it = (not is_plan) or await _ask_recovery(esc, sink, control)

    if not run_it:
        await sink.emit(ev.log(f"recuperação pulada: {esc.skill}", "warn"))
        return False

    await sink.emit(ev.recovery_started(esc.skill, esc.reason))
    await _run_one_phase(esc.skill, story_key, cfg, Advisor, sink, control, dry_run=False)
    ctx = GitContext(story_id=story_key, epic_id=epic_id)
    await apply_phase(cfg.phase(esc.skill), runner, ctx, sink)
    return True


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

    recoveries = 0
    iters = 0
    while (phase := status.next_phase(story_key)) is not None:
        control.raise_if_stopped()
        iters += 1
        if iters > cfg.max_phase_iters_per_story:
            # rede de segurança: se o status regredir (ex.: merge deixou o base
            # local defasado) a story re-rodaria sem fim. Aborta p/ não queimar tokens.
            await sink.emit(ev.error(
                f"story {story_key}: possível loop de fases (>{cfg.max_phase_iters_per_story} "
                "iterações) — abortando a story para não gastar tokens"))
            break
        target = phase.next_status
        done_pred = lambda t=target: status.story_status(story_key) == t
        esc = await _run_one_phase(phase.skill, story_key, cfg, Advisor, sink, control,
                                   dry_run, done_pred)

        # Escalação do advisor: roda skill de recuperação (ou pausa) ANTES de
        # avançar o status — senão marcaríamos como feito algo não resolvido.
        if esc is not None and not dry_run:
            if recoveries >= cfg.max_recoveries_per_story:
                await sink.emit(ev.error(
                    f"teto de recuperações ({cfg.max_recoveries_per_story}) atingido "
                    f"em {story_key}; seguindo sem recuperar"))
            else:
                recoveries += 1
                if await _handle_recovery(esc, story_key, epic_id, cfg, runner, sink, control):
                    continue  # recuperação rodou -> re-avalia o status (não força avanço)
                # pulada -> cai no fluxo normal (aceita o resultado da fase)

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
        esc = await _run_one_phase(RETROSPECTIVE, str(epic_id), cfg, Advisor, sink, control,
                                   dry_run, retro_done)
        if esc is not None and not dry_run:
            await _handle_recovery(esc, retro_key, str(epic_id), cfg, runner, sink, control)
        try:
            status.set_status(retro_key, "done")
            await sink.emit(ev.status_changed(retro_key, "done"))
        except KeyError:
            await sink.emit(ev.log(f"'{retro_key}' não existe no sprint-status; pulando", "warn"))
        ctx = GitContext(story_id="", epic_id=str(epic_id))
        await apply_phase(cfg.phase(RETROSPECTIVE), runner, ctx, sink)

        # epic concluída (stories + retrospective) -> vira o rótulo epic-N para done,
        # senão ele fica preso em "in-progress" e a UI mostra status enganoso.
        epic_label = status.epic_key(epic_id)
        try:
            frm = status.story_status(epic_label)
            if frm != "done":
                status.set_status(epic_label, "done")
                await sink.emit(ev.status_changed(epic_label, "done", frm))
        except KeyError:
            pass  # nem todo sprint-status tem o rótulo epic-N
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
