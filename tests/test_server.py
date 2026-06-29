"""Testes de integração do backend (FastAPI TestClient + WebSocket)."""

import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from autopilot import server


@pytest.fixture
def client(tmp_path: Path, monkeypatch):
    # isola o registro de projetos num arquivo temporário
    monkeypatch.setattr(server, "_APP_DIR", tmp_path)
    monkeypatch.setattr(server, "_PROJECTS_FILE", tmp_path / "projects.json")
    return TestClient(server.create_app())


@pytest.fixture
def project(client, fixture_project: Path):
    return client.post("/projects", json={"path": str(fixture_project)}).json()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json()["ok"] is True


def test_detect(client, project):
    d = client.get(f"/projects/{project['id']}/detect").json()
    assert d["sprint_status_exists"] is True
    assert {e["epic"] for e in d["epics"]} == {7, 8}


def test_status_includes_runnable(client, project):
    epics = client.get(f"/projects/{project['id']}/status").json()["epics"]
    e7 = next(e for e in epics if e["epic"] == 7)
    by_key = {s["key"]: s for s in e7["stories"]}
    assert by_key["7-2-create-api"]["runnable"] is True
    assert by_key["7-3-build-ui"]["runnable"] is False


def test_run_out_of_order_rejected(client, project):
    r = client.post("/run", json={
        "project_id": project["id"], "scope": "story",
        "id": "7-3-build-ui", "dry_run": True, "safe": True})
    assert r.status_code == 400
    assert "fora de ordem" in r.json()["detail"]


def test_run_dry_run_streams_events(client, project):
    r = client.post("/run", json={
        "project_id": project["id"], "scope": "story",
        "id": "7-2-create-api", "dry_run": True, "safe": True})
    assert r.status_code == 200

    # espera o run (dry) terminar — polls dão tempo ao loop
    for _ in range(50):
        if client.get("/health").json()["running"] is False:
            break
        time.sleep(0.05)

    kinds: list[str] = []
    with client.websocket_connect("/ws") as ws:
        for _ in range(200):
            ev = ws.receive_json()
            kinds.append(ev["kind"])
            if ev["kind"] == "run_ended":
                break
    assert "run_started" in kinds and "run_ended" in kinds
    assert "phase_started" in kinds


def test_config_get_and_set(client, tmp_project: Path):
    p = client.post("/projects", json={"path": str(tmp_project)}).json()
    cfg = client.get(f"/projects/{p['id']}/config").json()
    assert "advisor_prompt" in cfg and "phases" in cfg
    new = {
        "advisor_prompt": "PROMPT CUSTOM DE TESTE",
        "phases": {"bmad-code-review": {"git": [{"commit": "review: {story_id}"}]}},
    }
    assert client.post(f"/projects/{p['id']}/config", json=new).json()["ok"]
    assert (tmp_project / "autopilot.yaml").exists()
    cfg2 = client.get(f"/projects/{p['id']}/config").json()
    assert cfg2["advisor_prompt"] == "PROMPT CUSTOM DE TESTE"
