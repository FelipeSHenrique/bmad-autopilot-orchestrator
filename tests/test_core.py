"""Testes do núcleo determinístico (status, router, detect, ordering, events)."""

import asyncio
from pathlib import Path

from autopilot import router
from autopilot.detect import detect
from autopilot.events import EventSink, RunControl, StopRequested, log
from autopilot.status import SprintStatus, parse_story_key


# ---- status ------------------------------------------------------------
def test_parse_story_key():
    s = parse_story_key("7-10-polish")
    assert s and s.epic == 7 and s.num == 10 and s.slug == "polish"
    assert parse_story_key("epic-7") is None
    assert parse_story_key("epic-7-retrospective") is None


def test_numeric_ordering(sprint_status_file: Path):
    ss = SprintStatus(sprint_status_file)
    keys = [s.key for s in ss.epic_stories(7)]
    assert keys == ["7-1-define-schema", "7-2-create-api", "7-3-build-ui", "7-10-polish"]


def test_next_phase_and_epic_complete(sprint_status_file: Path):
    ss = SprintStatus(sprint_status_file)
    assert ss.next_phase("7-1-define-schema") is None           # done
    assert ss.next_phase("7-2-create-api").skill == "bmad-dev-story"
    assert ss.epic_complete(7) is False


def test_set_status_preserves_comments(tmp_project: Path):
    f = tmp_project / "_bmad-output/implementation-artifacts/sprint-status.yaml"
    ss = SprintStatus(f)
    ss.set_status("7-2-create-api", "review")
    out = f.read_text()
    assert "# STATUS DEFINITIONS:" in out
    assert "7-2-create-api: review" in out
    assert "7-10-polish: backlog" in out


# ---- router ------------------------------------------------------------
def test_lifecycle_from_backlog():
    steps = router.lifecycle(router.BACKLOG)
    assert [p.skill for p in steps] == [
        "bmad-create-story", "bmad-dev-story", "bmad-code-review",
    ]
    assert router.lifecycle(router.DONE) == []


# ---- ordering ----------------------------------------------------------
def test_story_runnable(sprint_status_file: Path):
    ss = SprintStatus(sprint_status_file)
    assert ss.story_runnable("7-2-create-api")[0] is True       # 7-1 done
    assert ss.story_runnable("7-3-build-ui")[0] is False        # 7-2 não done
    assert ss.story_runnable("7-1-define-schema")[0] is False   # já done


def test_epic_runnable(sprint_status_file: Path):
    ss = SprintStatus(sprint_status_file)
    assert ss.epic_runnable(7)[0] is True
    assert ss.epic_runnable(8)[0] is False                      # epic 7 incompleta


def test_epic_runnable_when_complete_but_retro_pending(tmp_project: Path):
    f = tmp_project / "_bmad-output/implementation-artifacts/sprint-status.yaml"
    ss = SprintStatus(f)
    # marca todas as stories da epic 7 como done; retrospective continua 'optional'
    for s in ss.epic_stories(7):
        ss.set_status(s.key, "done")
    assert ss.epic_complete(7) is True
    assert ss.epic_runnable(7)[0] is True       # ainda runnable: falta a retrospective
    ss.set_status("epic-7-retrospective", "done")
    assert ss.epic_runnable(7)[0] is False      # agora sim, nada a fazer


# ---- detect ------------------------------------------------------------
def test_detect(fixture_project: Path):
    d = detect(fixture_project)
    assert d["sprint_status_exists"] is True
    assert {e["epic"] for e in d["epics"]} == {7, 8}


# ---- events / RunControl ----------------------------------------------
def test_event_sink_callback_and_queue():
    sink = EventSink()
    seen = []
    sink.add_callback(lambda e: seen.append(e.kind))

    async def go():
        q = sink.subscribe()
        await sink.emit(log("oi"))
        ev = await q.get()
        return ev.kind

    assert asyncio.run(go()) == "log"
    assert seen == ["log"]
    assert len(sink.history) == 1


def test_run_control_stop_and_gate():
    async def go():
        c = RunControl()
        await c.gate()           # rodando: não bloqueia
        c.stop()
        raised = False
        try:
            c.raise_if_stopped()
        except StopRequested:
            raised = True
        return raised

    assert asyncio.run(go()) is True
