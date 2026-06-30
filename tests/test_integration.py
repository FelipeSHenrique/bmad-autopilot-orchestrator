"""Integração worker → advisor com o ClaudeSDKClient fakeado (sem tokens)."""

import asyncio
from pathlib import Path

import pytest

import fakes
from autopilot.config import CORRECT_COURSE, QUICK_DEV, config_for_project, safe_phases
from autopilot.events import EventSink, RunControl
from autopilot.loop import run as run_loop
from autopilot.status import SprintStatus

SS_REL = "_bmad-output/implementation-artifacts/sprint-status.yaml"


def test_worker_advisor_flow(git_project: Path, fake_claude):
    rec = fake_claude
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    decisions: list[dict] = []
    sink.add_callback(lambda e: (kinds.append(e.kind),
                                 decisions.append(e.data) if e.kind == "advisor_decision" else None))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    # 7-2 (ready-for-dev) → dev-story + code-review = 2 fases
    assert rec.connects.count("worker") == 2
    assert rec.connects.count("advisor") == 2
    assert rec.disconnects.count("worker") == 2
    assert rec.disconnects.count("advisor") == 2

    # o worker chamou AskUserQuestion e o advisor respondeu (rota estruturada)
    assert rec.answers == {fakes.QUESTIONS[0]["question"]: "Repository pattern"}
    assert len(decisions) == 2
    assert all("Repository pattern" in str(d["decision"]) for d in decisions)
    assert all(d["phase"] in ("bmad-dev-story", "bmad-code-review") for d in decisions)

    # status avançou até done
    ss = SprintStatus(git_project / SS_REL)
    assert ss.story_status("7-2-create-api") == "done"

    # memória do advisor + log de decisões gravados
    assert (git_project / ".autopilot/advisor-memory.md").read_text().strip()
    assert (git_project / ".autopilot/logs/decisions.jsonl").exists()
    assert kinds[-1] == "run_ended"


def test_undetected_pause_gets_one_nudge(git_project: Path, fake_claude):
    """(B) Sinal estrutural: o worker encerra um turno não-done com texto que NÃO
    parece pergunta ("…concluí.") -> recebe UM empurrão (nudge) e a fase conclui.
    Não pode loopar nem consultar o advisor por texto (decisão é só a estruturada)."""
    rec = fake_claude
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    sink.add_callback(lambda e: None)

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    # exatamente um nudge por fase (2 fases) -> não houve loop
    nudges = [p for (role, p) in rec.queries
              if role == "worker" and "AskUserQuestion" in p]
    assert len(nudges) == 2
    # status concluiu mesmo assim (orquestrador finaliza após o break)
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_epic_marks_label_done(git_project: Path, fake_claude):
    """Ao completar a epic (todas as stories + retrospective), o rótulo epic-N
    deve virar 'done' — senão a UI mostra 'in-progress' enganoso."""
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    sink.add_callback(lambda e: None)

    asyncio.run(run_loop(cfg, story=None, epic="7", dry_run=False,
                         sink=sink, control=RunControl()))

    ss = SprintStatus(git_project / SS_REL)
    assert ss.story_status("epic-7-retrospective") == "done"
    assert ss.story_status("epic-7") == "done"   # rótulo da epic atualizado
    assert ss.epic_complete(7)


def test_recovery_tiered_runs_quick_dev(git_project: Path, fake_claude):
    """Política 'tiered': escalação para quick-dev (código) roda AUTÔNOMA, sem pausar."""
    rec = fake_claude
    rec.advisor_escalate = QUICK_DEV
    cfg = config_for_project(git_project, phases=safe_phases())  # recovery_policy=tiered
    sink = EventSink()
    kinds: list[str] = []
    started: list[str] = []
    sink.add_callback(lambda e: (kinds.append(e.kind),
                                 started.append(e.data.get("skill"))
                                 if e.kind == "recovery_started" else None))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert QUICK_DEV in started                  # rodou o quick-dev autônomo
    assert "recovery_recommended" not in kinds    # tiered NÃO pausa p/ quick-dev
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_recovery_correct_course_pauses(git_project: Path, fake_claude):
    """Política 'tiered': escalação para correct-course (plano) PAUSA e espera o humano."""
    rec = fake_claude
    rec.advisor_escalate = CORRECT_COURSE
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    started: list[str] = []
    sink.add_callback(lambda e: (kinds.append(e.kind),
                                 started.append(e.data.get("skill"))
                                 if e.kind == "recovery_started" else None))
    control = RunControl()

    async def go():
        task = asyncio.create_task(run_loop(
            cfg, story="7-2-create-api", epic=None, dry_run=False,
            sink=sink, control=control))
        for _ in range(200):                       # espera a pausa aparecer
            if "recovery_recommended" in kinds:
                break
            await asyncio.sleep(0.02)
        assert "recovery_recommended" in kinds      # pausou, esperando escolha
        assert not task.done()                      # bloqueado no wait_recovery_choice
        control.choose_recovery("run")              # humano aprova rodar
        await task

    asyncio.run(go())
    assert CORRECT_COURSE in started
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_recovery_correct_course_skip(git_project: Path, fake_claude):
    """Pular a recuperação: não roda a skill, aceita o resultado da fase e segue."""
    rec = fake_claude
    rec.advisor_escalate = CORRECT_COURSE
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))
    control = RunControl()

    async def go():
        task = asyncio.create_task(run_loop(
            cfg, story="7-2-create-api", epic=None, dry_run=False,
            sink=sink, control=control))
        for _ in range(200):
            if "recovery_recommended" in kinds:
                break
            await asyncio.sleep(0.02)
        control.choose_recovery("skip")
        await task

    asyncio.run(go())
    assert "recovery_started" not in kinds          # não rodou a recuperação
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_recovery_cap_prevents_loop(git_project: Path, fake_claude):
    """Advisor escalando SEMPRE não pode loopar: o teto max_recoveries_per_story corta."""
    rec = fake_claude
    rec.advisor_escalate = QUICK_DEV
    rec.advisor_escalate_once = False               # escala em toda decisão
    cfg = config_for_project(git_project, phases=safe_phases())
    cfg.max_recoveries_per_story = 2
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    async def go():
        await asyncio.wait_for(
            run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                     sink=sink, control=RunControl()),
            timeout=30)   # se loopar de verdade, estoura aqui

    asyncio.run(go())
    assert kinds[-1] == "run_ended"
    assert sum(1 for k in kinds if k == "recovery_started") <= 2  # respeitou o teto


def test_token_limit_halts_cleanly(git_project: Path, fake_claude):
    """Limite de tokens/rate-limit no meio do run → encerra LIMPO (sem exceção),
    emite token_limit + run_ended rotulado, e NÃO avança o sprint-status."""
    rec = fake_claude
    rec.token_limit_mode = "ratelimit"   # worker recebe RateLimitEvent(rejected)
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    reasons: list[str] = []
    sink.add_callback(lambda e: (kinds.append(e.kind),
                                 reasons.append(e.data.get("reason", "")) if e.kind == "run_ended" else None))

    before = SprintStatus(git_project / SS_REL).story_status("7-2-create-api")
    # não deve levantar exceção (halt limpo)
    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "token_limit" in kinds                 # evento de limite emitido
    assert kinds[-1] == "run_ended"
    assert any("limite de tokens" in r for r in reasons)
    # estado preservado: a story NÃO avançou (retoma re-rodando depois)
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == before


def test_token_limit_via_result_error(git_project: Path, fake_claude):
    """Mesmo halt limpo quando o sinal vem como ResultMessage(is_error, HTTP 429)."""
    rec = fake_claude
    rec.token_limit_mode = "result429"
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "token_limit" in kinds
    assert kinds[-1] == "run_ended"


def test_stop_cancels_mid_turn(git_project: Path, fake_claude):
    rec = fake_claude
    rec.worker_mode = "block"   # worker trava no meio do turno
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))
    control = RunControl()

    async def go():
        task = asyncio.create_task(run_loop(
            cfg, story="7-2-create-api", epic=None, dry_run=False,
            sink=sink, control=control))
        await asyncio.sleep(0.1)     # deixa o worker conectar e bloquear
        task.cancel()                # equivalente ao que o RunManager.stop faz
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(go())

    # o worker conectou e, no cancel, o finally desconectou (limpou a sessão claude)
    assert "worker" in rec.connects
    assert "worker" in rec.disconnects
    # e o run encerrou sinalizando parada
    assert "run_ended" in kinds


def test_phase_does_not_loop_forever(git_project: Path, fake_claude):
    """Skill tagarela que nunca conclui (pergunta todo turno) deve PARAR no teto
    de decisões/turnos, em vez de loopar infinito (bug do retrospective)."""
    rec = fake_claude
    rec.worker_mode = "loop"
    cfg = config_for_project(git_project, phases=safe_phases())
    cfg.max_turns_per_phase = 20  # teto duro de turnos
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    async def go():
        await asyncio.wait_for(
            run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                     sink=sink, control=RunControl()),
            timeout=30,  # se loopar de verdade, estoura aqui e o teste falha
        )

    asyncio.run(go())
    # terminou (não travou) e cada fase respeitou o teto de decisões
    assert kinds[-1] == "run_ended"
    decisions = sum(1 for k in kinds if k == "advisor_decision")
    # 2 fases (dev-story, code-review) × teto(12) + folga
    assert decisions <= 2 * cfg.max_decisions_per_phase + 2
