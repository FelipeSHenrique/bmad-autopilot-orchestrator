"""Orquestração: itera fases por story, roda retrospective ao fim da epic,
aplica regras de git e emite eventos. Respeita pause/stop e checkpoints
humanos (via RunControl)."""

from __future__ import annotations

import asyncio

from . import events as ev
from . import router
from .advisor import Advisor
from .config import CORRECT_COURSE, RETROSPECTIVE, Config
from .events import (
    ConnectionLost,
    Escalation,
    EventSink,
    RunControl,
    StopRequested,
    TokenLimitReached,
)
from .git_rules import GitContext, GitRunner, apply_phase
from .status import SprintStatus, parse_story_key
from .worker import run_phase


async def _emit_status_diffs(status: SprintStatus, sink: EventSink, last: dict[str, str]) -> None:
    """Emite status_changed para cada chave que mudou no sprint-status desde `last`
    (atualiza `last`). Fonte ÚNICA de visibilidade de status — com `from` correto e
    sem duplicar. Usada tanto pelo flush após cada fase quanto pelo poller ao vivo."""
    try:
        cur = status.development_status()
    except Exception:
        return
    for key, to in cur.items():
        frm = last.get(key)
        if frm != to:
            last[key] = to   # atualiza ANTES do await (evita corrida poller x flush)
            await sink.emit(ev.status_changed(key, to, frm))


async def _status_poller(
    status: SprintStatus, sink: EventSink, last: dict[str, str], interval: float
) -> None:
    """Roda em paralelo ao run: a cada `interval`s, emite as mudanças de status que
    a skill gravou no arquivo (ex.: in-progress) — visibilidade ao vivo no app."""
    while True:
        await asyncio.sleep(interval)
        await _emit_status_diffs(status, sink, last)


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
        final_text, sid = await run_phase(skill, target_id, cfg, advisor, sink, control,
                                          dry_run=False, done_predicate=done_predicate)
        if cfg.enable_gate:
            await _run_gate(skill, target_id, cfg, advisor, sink, control, final_text)
        return advisor.last_escalation


async def _run_gate(
    skill: str, target_id: str, cfg: Config, advisor, sink: EventSink,
    control: RunControl, final_text: str,
) -> None:
    """Gate de conclusão: o advisor valida o resultado da fase e diz se pode avançar.

    Na REPROVAÇÃO NÃO re-executa a fase automaticamente — re-rodar uma fase cara
    (ex.: code-review com camadas adversariais) queima tokens e atropela o trabalho
    em andamento da skill. Em vez disso, PAUSA e deixa o humano decidir: aprovar e
    avançar, ou parar (e corrigir manualmente re-rodando). Como o worker agora deixa
    o trabalho assíncrono concluir (item 2), a fase costuma passar de primeira e o
    gate raramente bloqueia."""
    v = await advisor.review_phase(skill, target_id, final_text)
    blockers = v.get("blockers", [])
    await sink.emit(ev.gate_review(skill, target_id, v["ok"], blockers))
    if v["ok"]:
        return
    await sink.emit(ev.checkpoint_hit(
        f"gate bloqueou {skill}:{target_id} — {'; '.join(blockers) or 'pendências'}"))
    await control.wait_approval()  # aprovar = avança; stop = encerra


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
    sink: EventSink, control: RunControl, dry_run: bool, last: dict[str, str],
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

        # A skill do BMAD é a DONA do status (escrita): o orquestrador só grava como
        # backstop, se a skill não avançou. A visibilidade fica por conta do
        # _emit_status_diffs (flush determinístico após a fase + poller ao vivo).
        if status.story_status(story_key) != phase.next_status:
            status.set_status(story_key, phase.next_status)
        await _emit_status_diffs(status, sink, last)

        ctx = GitContext(story_id=story_key, epic_id=epic_id)
        await apply_phase(cfg.phase(phase.skill), runner, ctx, sink)

        if cfg.autonomy.human_checkpoint == "end-of-story" and phase.next_status == "done":
            await _checkpoint(f"story {story_key} concluída", cfg, sink, control)


async def _finalize_epic(
    epic_id: str, cfg: Config, status: SprintStatus, runner: GitRunner,
    sink: EventSink, control: RunControl, last: dict[str, str],
) -> bool:
    """Fecha a epic: garante a retrospective e vira o rótulo epic-N para done.

    Robusto às duas ordens em que a retro pode acontecer:
    - o orquestrador roda a retrospective aqui (fluxo padrão), OU
    - a skill de code-review já encadeou a retrospective inline (o advisor aceitou o
      next-step "Run epic-N retrospective") — nesse caso `retro_key` já está done e só
      falta virar o rótulo da epic. O flip fica FORA do bloco "retro não feita" de
      propósito: senão o early-return quando a retro já está done deixaria a epic presa
      em in-progress. Retorna True se rodou a retro e/ou fechou o rótulo da epic."""
    if not status.epic_complete(epic_id):
        return False
    retro_key = status.retrospective_key(epic_id)
    epic_label = status.epic_key(epic_id)
    epic_open = status.story_status(epic_label) not in (None, "done")
    if status.story_status(retro_key) == "done" and not epic_open:
        return False  # nada a fazer: retro feita E rótulo já fechado

    if status.story_status(retro_key) != "done":
        if cfg.autonomy.human_checkpoint == "retrospective":
            await _checkpoint(f"epic {epic_id} concluída", cfg, sink, control)
        retro_done = lambda: status.story_status(retro_key) == "done"
        esc = await _run_one_phase(RETROSPECTIVE, str(epic_id), cfg, Advisor, sink, control,
                                   False, retro_done)
        if esc is not None:
            await _handle_recovery(esc, retro_key, str(epic_id), cfg, runner, sink, control)
        if status.story_status(retro_key) != "done":   # backstop (a retro costuma gravar)
            try:
                status.set_status(retro_key, "done")
            except KeyError:
                await sink.emit(ev.log(f"'{retro_key}' não existe no sprint-status; pulando", "warn"))

    # vira o rótulo epic-N para done SEMPRE que a epic está completa e a retro feita —
    # inclusive quando a retro rodou inline na skill (early-return acima não se aplica).
    if status.story_status(epic_label) not in (None, "done"):
        try:
            status.set_status(epic_label, "done")
        except KeyError:
            pass
    ctx = GitContext(story_id="", epic_id=str(epic_id))
    await apply_phase(cfg.phase(RETROSPECTIVE), runner, ctx, sink)
    await _emit_status_diffs(status, sink, last)
    return True


async def run_epic(
    epic_id: str, cfg: Config, status: SprintStatus, runner: GitRunner,
    sink: EventSink, control: RunControl, dry_run: bool, last: dict[str, str],
) -> None:
    stories = status.epic_stories(epic_id)
    if not stories:
        raise SystemExit(f"nenhuma story encontrada para a epic {epic_id}")
    await sink.emit(ev.log(f"epic {epic_id}: {len(stories)} stories"))
    for s in stories:
        control.raise_if_stopped()
        await process_story(s.key, cfg, status, runner, sink, control, dry_run, last)

    if dry_run:
        await sink.emit(ev.log(f"[dry-run] ao completar a epic rodaria {RETROSPECTIVE}"))
        return
    if status.epic_complete(epic_id):
        await _finalize_epic(epic_id, cfg, status, runner, sink, control, last)
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
    if not dry_run:
        # não deixa os internals do orquestrador entrarem no repo do usuário
        runner.ignore_locally(".autopilot/")

    # Visibilidade de status ao vivo: snapshot inicial + poller do sprint-status
    # (só em run real; em dry-run o arquivo não muda e usamos emits simulados).
    last: dict[str, str] = {}
    poller: asyncio.Task | None = None
    if not dry_run:
        try:
            last = status.development_status()
        except Exception:
            last = {}
        poller = asyncio.create_task(
            _status_poller(status, sink, last, cfg.status_poll_interval))
    try:
        if story:
            await process_story(story, cfg, status, runner, sink, control, dry_run, last)
            # Se esta foi a ÚLTIMA story da epic, já roda a retrospective e fecha a epic.
            if not dry_run and cfg.auto_retrospective:
                parsed = parse_story_key(story)
                if parsed:
                    await _finalize_epic(str(parsed.epic), cfg, status, runner, sink, control, last)
        elif epic:
            await run_epic(epic, cfg, status, runner, sink, control, dry_run, last)
        else:
            raise SystemExit("informe --story ou --epic")
        await sink.emit(ev.run_ended(True))
    except StopRequested:
        await sink.emit(ev.run_ended(False, "parado pelo usuário"))
    except TokenLimitReached:
        # halt limpo: estado preservado no sprint-status; retoma re-rodando.
        await sink.emit(ev.run_ended(False, "limite de tokens — pausado (retome re-rodando)"))
    except ConnectionLost as exc:
        # rede caiu: halt limpo; a sessão fica retomável (↻) quando a conexão voltar.
        await sink.emit(ev.connection_lost(str(exc)))
        await sink.emit(ev.run_ended(False, "sem conexão — pausado (retome com ↻ quando a rede voltar)"))
    except asyncio.CancelledError:
        await sink.emit(ev.run_ended(False, "parado pelo usuário"))
        raise
    except Exception as exc:  # noqa: BLE001 — reporta e encerra o run limpo
        await sink.emit(ev.error(str(exc)))
        await sink.emit(ev.run_ended(False, str(exc)))
        raise
    finally:
        if poller is not None:
            poller.cancel()
            try:
                await poller
            except (asyncio.CancelledError, Exception):
                pass
