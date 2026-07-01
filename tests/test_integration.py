"""Integração worker → advisor com o ClaudeSDKClient fakeado (sem tokens)."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
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


def test_epic_closes_when_retro_already_done_inline(git_project: Path, fake_claude):
    """Regressão: se a skill de code-review encadeou a retrospective INLINE (retro já
    'done' quando o orquestrador chega em _finalize_epic), o rótulo epic-N ainda tem
    que virar 'done' — o early-return não pode deixar a epic presa em in-progress."""
    ss0 = SprintStatus(git_project / SS_REL)
    for s in ("7-1-define-schema", "7-2-create-api", "7-3-build-ui", "7-10-polish"):
        ss0.set_status(s, "done")
    ss0.set_status("epic-7-retrospective", "done")   # retro JÁ feita (inline na skill)
    ss0.set_status("epic-7", "in-progress")          # rótulo ainda aberto

    rec = fake_claude
    sink = EventSink()
    skills: list[str] = []
    sink.add_callback(lambda e: skills.append(e.data.get("skill"))
                      if e.kind == "phase_started" else None)
    cfg = config_for_project(git_project, phases=safe_phases())

    asyncio.run(run_loop(cfg, story=None, epic="7", dry_run=False,
                         sink=sink, control=RunControl()))

    ss = SprintStatus(git_project / SS_REL)
    assert ss.story_status("epic-7") == "done"          # fechou mesmo com retro inline
    assert "bmad-retrospective" not in skills           # NÃO re-rodou a retro (já done)


def test_autopilot_internals_not_committed(git_project: Path, fake_claude):
    """Os internals do orquestrador (`.autopilot/`) não podem entrar no repo do
    usuário: o run adiciona `.autopilot/` ao .git/info/exclude e o `git add -A`
    das fases não o comita."""
    import subprocess
    cfg = config_for_project(git_project, phases=safe_phases())
    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=EventSink(), control=RunControl()))

    assert (git_project / ".autopilot").exists()          # existe no disco
    tracked = subprocess.run(["git", "ls-files"], cwd=git_project,
                             capture_output=True, text=True).stdout
    assert ".autopilot/" not in tracked                   # mas NÃO é rastreado
    exclude = (git_project / ".git/info/exclude").read_text()
    assert ".autopilot/" in exclude                       # está no ignore local


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


RESUME_REL = ".autopilot/resume.json"


def _markers(git_project: Path) -> dict:
    p = git_project / RESUME_REL
    return json.loads(p.read_text()) if p.exists() else {}


def test_resume_marker_cleared_on_completion(git_project: Path, fake_claude):
    """Run normal: cada fase usa sessão FRESH (session_id, sem resume) e o
    marcador é removido ao concluir — nada retomável sobra."""
    rec = fake_claude
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    sink.add_callback(lambda e: None)

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    # todas as sessões do worker foram fresh (session_id setado, resume None)
    assert rec.sessions and all(s["resume"] is None and s["session_id"] for s in rec.sessions)
    # marcador da story foi limpo na conclusão
    assert "7-2-create-api" not in _markers(git_project)


def test_resume_continues_after_interruption(git_project: Path, fake_claude):
    """Limite de tokens no meio → marcador permanece → re-rodar RESUME a MESMA
    sessão (options.resume) com phase_resumed, e conclui."""
    rec = fake_claude
    cfg = config_for_project(git_project, phases=safe_phases())

    # run 1: interrompido por limite no 1º turno do worker (dev-story de 7-2)
    rec.token_limit_mode = "ratelimit"
    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=EventSink(), control=RunControl()))

    markers = _markers(git_project)
    assert "7-2-create-api" in markers           # marcador preservado
    saved_sid = markers["7-2-create-api"]["session_id"]
    assert saved_sid

    # run 2: sem limite -> deve retomar a sessão salva
    rec.token_limit_mode = None
    rec.sessions.clear()
    kinds: list[str] = []
    sink = EventSink()
    sink.add_callback(lambda e: kinds.append(e.kind))
    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "phase_resumed" in kinds
    assert any(s["resume"] == saved_sid for s in rec.sessions)   # reabriu a MESMA sessão
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"
    assert "7-2-create-api" not in _markers(git_project)         # limpo ao concluir


def test_resume_ttl_expired_starts_fresh(git_project: Path, fake_claude):
    """Marcador além do TTL é ignorado: começa do zero (sessão nova), sem resume."""
    rec = fake_claude
    cfg = config_for_project(git_project, phases=safe_phases())
    cfg.resume_ttl_hours = 24

    old_ts = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    p = git_project / RESUME_REL
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"7-2-create-api": {
        "skill": "bmad-dev-story", "session_id": "OLD-SESSION", "ts": old_ts}}))

    rec.sessions.clear()
    kinds: list[str] = []
    sink = EventSink()
    sink.add_callback(lambda e: kinds.append(e.kind))
    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert not any(s["resume"] == "OLD-SESSION" for s in rec.sessions)  # não retomou o velho
    assert "phase_resumed" not in kinds


def test_connection_lost_halts_cleanly(git_project: Path, fake_claude):
    """Queda de rede (ResultMessage 502) no meio → halt LIMPO (connection_lost +
    run_ended), marcador preservado (retomável) e status NÃO avança."""
    rec = fake_claude
    rec.token_limit_mode = "network"
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "connection_lost" in kinds
    assert kinds[-1] == "run_ended"
    assert "7-2-create-api" in _markers(git_project)     # sessão retomável (↻)
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "ready-for-dev"


def test_connection_lost_via_exception(git_project: Path, fake_claude):
    """Mesmo halt limpo quando a rede cai como EXCEÇÃO (ConnectionError do CLI)."""
    rec = fake_claude
    rec.token_limit_mode = "neterror"
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "connection_lost" in kinds
    assert kinds[-1] == "run_ended"
    assert "7-2-create-api" in _markers(git_project)     # retomável, não perdeu o trabalho


def test_status_changed_emitted_for_visibility(git_project: Path, fake_claude):
    """As transições de status devem ser EMITIDAS ao vivo (visibilidade no app),
    mesmo a skill sendo a dona da escrita do sprint-status."""
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    transitions: list[tuple] = []
    sink.add_callback(lambda e: transitions.append((e.data.get("from"), e.data.get("to")))
                      if e.kind == "status_changed" else None)

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    # 7-2 (ready-for-dev) → dev-story (review) → code-review (done)
    assert ("ready-for-dev", "review") in transitions
    assert ("review", "done") in transitions


def test_last_story_auto_triggers_retrospective(git_project: Path, fake_claude):
    """Rodar a ÚLTIMA story da epic (scope story) deve disparar a retrospective e
    fechar a epic automaticamente (auto_retrospective)."""
    ss = SprintStatus(git_project / SS_REL)
    for k in ("7-2-create-api", "7-3-build-ui"):   # 7-1 já é done -> falta só a 7-10
        ss.set_status(k, "done")
    cfg = config_for_project(git_project, phases=safe_phases())   # auto_retrospective default True
    sink = EventSink()
    skills: list[str] = []
    sink.add_callback(lambda e: skills.append(e.data.get("skill"))
                      if e.kind == "phase_started" else None)

    asyncio.run(run_loop(cfg, story="7-10-polish", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "bmad-retrospective" in skills          # retro rodou automaticamente
    ss2 = SprintStatus(git_project / SS_REL)
    assert ss2.story_status("epic-7-retrospective") == "done"
    assert ss2.story_status("epic-7") == "done"


def test_last_story_no_auto_retro_when_disabled(git_project: Path, fake_claude):
    """Com auto_retrospective=False, a última story NÃO dispara a retrospective."""
    ss = SprintStatus(git_project / SS_REL)
    for k in ("7-2-create-api", "7-3-build-ui"):
        ss.set_status(k, "done")
    cfg = config_for_project(git_project, phases=safe_phases(), auto_retrospective=False)
    sink = EventSink()
    skills: list[str] = []
    sink.add_callback(lambda e: skills.append(e.data.get("skill"))
                      if e.kind == "phase_started" else None)

    asyncio.run(run_loop(cfg, story="7-10-polish", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "bmad-retrospective" not in skills
    assert SprintStatus(git_project / SS_REL).story_status("epic-7-retrospective") != "done"


def test_gate_passes_advances(git_project: Path, fake_claude):
    """Gate aprova (default) → emite gate_review e a fase avança normalmente."""
    cfg = config_for_project(git_project, phases=safe_phases())   # enable_gate default True
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "gate_review" in kinds
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_gate_no_go_corrects_in_same_session(git_project: Path, fake_claude):
    """No-go → o prompt de correção do advisor vai pra MESMA sessão (resume) e o
    gate revalida; depois a fase avança."""
    rec = fake_claude
    rec.gate_verdicts = [
        {"ok": False, "blockers": ["falta teste de regressão"],
         "corrections": "Adicione o teste de regressão do SET NULL."},
        {"ok": True, "blockers": [], "corrections": ""},
    ]
    cfg = config_for_project(git_project, phases=safe_phases())
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))

    asyncio.run(run_loop(cfg, story="7-2-create-api", epic=None, dry_run=False,
                         sink=sink, control=RunControl()))

    assert "gate_correcting" in kinds
    # a correção reabriu a MESMA sessão da fase (resume == session_id da 1ª sessão worker)
    assert rec.sessions[1]["resume"] == rec.sessions[0]["session_id"]
    # o prompt de correção do advisor foi injetado no worker
    assert any(role == "worker" and "Adicione o teste de regressão" in p
               for role, p in rec.queries)
    assert SprintStatus(git_project / SS_REL).story_status("7-2-create-api") == "done"


def test_gate_cap_pauses_for_human(git_project: Path, fake_claude):
    """Gate reprovando sempre → após max_gate_rounds, pausa pro humano (checkpoint);
    não loopa, e ao aprovar avança."""
    rec = fake_claude
    rec.gate_verdicts = [{"ok": False, "blockers": ["x"], "corrections": "fix"}] * 3
    cfg = config_for_project(git_project, phases=safe_phases())
    cfg.max_gate_rounds = 2
    sink = EventSink()
    kinds: list[str] = []
    sink.add_callback(lambda e: kinds.append(e.kind))
    control = RunControl()

    async def go():
        task = asyncio.create_task(run_loop(
            cfg, story="7-2-create-api", epic=None, dry_run=False,
            sink=sink, control=control))
        for _ in range(300):
            if "checkpoint_hit" in kinds:
                break
            await asyncio.sleep(0.02)
        assert "checkpoint_hit" in kinds      # pausou pro humano (teto)
        assert not task.done()
        control.approve()                      # aprova -> avança
        await asyncio.wait_for(task, timeout=10)

    asyncio.run(go())
    assert kinds.count("gate_review") >= 3     # revisou ≥3× antes de pausar


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
