"""Backend HTTP + WebSocket do autopilot (consumido pelo app macOS).

REST controla projetos/run; o WebSocket /ws faz streaming dos eventos do
run em tempo real. Reusa todo o core (loop/worker/advisor) via EventSink.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel

from . import detect as detect_mod
from . import events as ae
from . import resume as resume_mod
from .config import (
    DEFAULT_ADVISOR_PROMPT,
    Config,
    config_for_project,
    default_phases,
    load_project_overrides,
    phases_to_dict,
    project_overrides_path,
    safe_phases,
    save_project_overrides,
)
from .events import EventSink, RunControl
from .loop import run as run_loop
from .status import SprintStatus

# ---- registro de projetos (persistente) --------------------------------

_APP_DIR = Path.home() / "Library" / "Application Support" / "Autopilot"
_PROJECTS_FILE = _APP_DIR / "projects.json"


def _project_id(path: str) -> str:
    return hashlib.sha1(str(Path(path).expanduser()).encode()).hexdigest()[:12]


def _load_projects() -> list[dict[str, Any]]:
    if _PROJECTS_FILE.exists():
        try:
            return json.loads(_PROJECTS_FILE.read_text())
        except json.JSONDecodeError:
            return []
    return []


def _save_projects(projects: list[dict[str, Any]]) -> None:
    _APP_DIR.mkdir(parents=True, exist_ok=True)
    _PROJECTS_FILE.write_text(json.dumps(projects, indent=2, ensure_ascii=False))


def _find_project(pid: str) -> dict[str, Any]:
    for p in _load_projects():
        if p["id"] == pid:
            return p
    raise HTTPException(404, f"projeto {pid} não registrado")


# ---- gerenciador do run ativo ------------------------------------------


class RunManager:
    def __init__(self) -> None:
        self.sink = EventSink()
        self.control: RunControl | None = None
        self.task: asyncio.Task | None = None
        self.current: dict[str, Any] | None = None
        self.ws_count = 0
        self._deadman: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self.task is not None and not self.task.done()

    def start(self, cfg, scope: str, target: str, dry_run: bool) -> None:
        if self.running:
            raise HTTPException(409, "já existe um run ativo")
        self.sink.reset_history()
        self.control = RunControl(interactive_cli=False)
        self.current = {"scope": scope, "target": target, "dry_run": dry_run}
        story = target if scope == "story" else None
        epic = target if scope == "epic" else None

        async def _go():
            await run_loop(
                cfg, story=story, epic=epic, dry_run=dry_run,
                sink=self.sink, control=self.control,
            )

        self.task = asyncio.create_task(_go())

        def _done(_t: asyncio.Task) -> None:
            self.current = None

        self.task.add_done_callback(_done)

    def _stop_now(self) -> None:
        if self.control:
            self.control.stop()
        if self.task and not self.task.done():
            self.task.cancel()   # interrompe mesmo no meio de um turno do worker

    async def control_action(self, action: str) -> None:
        if not self.control:
            raise HTTPException(409, "nenhum run ativo")
        if action == "stop":
            self._stop_now()
        elif action == "pause":
            self.control.pause()
            await self.sink.emit(ae.run_paused("user"))
        elif action == "resume":
            self.control.resume()
            await self.sink.emit(ae.run_resumed())
        elif action == "approve":
            self.control.approve()              # checkpoint
            self.control.choose_recovery("run")  # ou: rodar a recuperação pendente
        elif action == "skip":
            self.control.choose_recovery("skip")  # pular a recuperação pendente
        else:
            raise HTTPException(400, "ação inválida")

    # ---- dead-man switch: para o run se o app desconectar -------------
    def ws_connect(self) -> None:
        self.ws_count += 1
        if self._deadman:
            self._deadman.cancel()
            self._deadman = None

    def ws_disconnect(self) -> None:
        self.ws_count = max(0, self.ws_count - 1)
        if self.ws_count == 0 and self.running:
            self._deadman = asyncio.create_task(self._deadman_stop())

    async def _deadman_stop(self) -> None:
        # Janela generosa: reconexões transitórias do app NÃO devem derrubar um
        # run ativo. Só paramos se o app ficar realmente ausente por bastante tempo.
        try:
            await asyncio.sleep(45)
        except asyncio.CancelledError:
            return
        if self.ws_count == 0 and self.running:
            await self.sink.emit(ae.log(
                "dead-man switch: app desconectado, parando o run para não gastar tokens",
                "warn"))
            self._stop_now()


# ---- API ----------------------------------------------------------------


class AddProject(BaseModel):
    path: str
    name: str | None = None


class RunRequest(BaseModel):
    project_id: str
    scope: str          # "story" | "epic"
    id: str
    dry_run: bool = False
    human_checkpoint: str = "none"
    safe: bool = True   # branch + commits locais; sem push/PR/merge
    fresh: bool = False  # "começar do zero": descarta a sessão retomável


class ControlRequest(BaseModel):
    action: str         # pause | resume | stop | approve | skip


def create_app() -> FastAPI:
    app = FastAPI(title="autopilot")
    mgr = RunManager()

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "running": mgr.running,
            "current": mgr.current,
            "paused": mgr.control.paused if mgr.control else False,
        }

    @app.get("/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return _load_projects()

    @app.post("/projects")
    async def add_project(body: AddProject) -> dict[str, Any]:
        path = str(Path(body.path).expanduser())
        if not Path(path).is_dir():
            raise HTTPException(400, "caminho não é um diretório")
        projects = _load_projects()
        pid = _project_id(path)
        if not any(p["id"] == pid for p in projects):
            projects.append({"id": pid, "path": path, "name": body.name or Path(path).name})
            _save_projects(projects)
        return next(p for p in projects if p["id"] == pid)

    @app.delete("/projects/{pid}")
    async def remove_project(pid: str) -> dict[str, bool]:
        projects = [p for p in _load_projects() if p["id"] != pid]
        _save_projects(projects)
        return {"ok": True}

    @app.get("/projects/{pid}/detect")
    async def detect_project(pid: str) -> dict[str, Any]:
        proj = _find_project(pid)
        info = detect_mod.detect(proj["path"])
        # anota resume_available por story/epic (sessão retomável dentro do TTL)
        cfg = config_for_project(proj["path"])
        targets = resume_mod.available_targets(cfg, cfg.resume_ttl_hours)
        for e in info.get("epics", []):
            stories = e.get("stories", [])
            for s in stories:
                s["resume_available"] = s.get("key") in targets
            e["resume_available"] = (str(e.get("epic")) in targets
                                     or any(s.get("resume_available") for s in stories))
        return info

    @app.get("/projects/{pid}/status")
    async def project_status(pid: str) -> dict[str, Any]:
        proj = _find_project(pid)
        info = detect_mod.detect(proj["path"])
        epics = info["epics"]
        # anota runnable por story/epic (p/ o app desabilitar os ▶ fora de ordem)
        ss_path = info["sprint_status_path"]
        if ss_path:
            ss = SprintStatus(Path(proj["path"]) / ss_path)
            for e in epics:
                e["runnable"], e["runnable_reason"] = ss.epic_runnable(e["epic"])
                for s in e["stories"]:
                    s["runnable"], s["runnable_reason"] = ss.story_runnable(s["key"])
        return {"epics": epics, "sprint_status_path": ss_path}

    @app.get("/projects/{pid}/config")
    async def get_config(pid: str) -> dict[str, Any]:
        proj = _find_project(pid)
        ov = load_project_overrides(proj["path"])
        phases = ov.get("phases") or default_phases()
        models = ov.get("models") or {}
        return {
            "advisor_prompt": ov.get("advisor_prompt") or DEFAULT_ADVISOR_PROMPT,
            "invoke_template": ov.get("invoke_template") or Config.invoke_template,
            "models": {
                "worker": models.get("worker") or "claude-opus-4-8",
                "advisor": models.get("advisor") or "claude-opus-4-8",
            },
            "human_checkpoint": ov.get("human_checkpoint") or "none",
            "recovery_policy": ov.get("recovery_policy") or "tiered",
            "enable_gate": ov.get("enable_gate", True),
            "auto_retrospective": ov.get("auto_retrospective", True),
            "phases": phases_to_dict(phases),
            "has_override_file": project_overrides_path(proj["path"]).exists(),
        }

    @app.post("/projects/{pid}/config")
    async def set_config(pid: str, body: dict[str, Any]) -> dict[str, bool]:
        proj = _find_project(pid)
        save_project_overrides(proj["path"], body)
        return {"ok": True}

    @app.post("/run")
    async def start_run(body: RunRequest) -> dict[str, Any]:
        proj = _find_project(body.project_id)
        info = detect_mod.detect(proj["path"])
        if not info["sprint_status_path"]:
            raise HTTPException(400, "sprint-status.yaml não encontrado no projeto")

        # Validação de ordem (bloquear play fora de ordem).
        ss = SprintStatus(Path(proj["path"]) / info["sprint_status_path"])
        if body.scope == "story":
            ok, reason = ss.story_runnable(body.id)
        else:
            ok, reason = ss.epic_runnable(body.id)
        if not ok:
            raise HTTPException(400, f"fora de ordem: {reason}")

        checkpoint = body.human_checkpoint
        if body.safe and checkpoint == "none":
            checkpoint = "end-of-story"   # no modo seguro, pausa p/ revisão

        # Overrides por projeto (<project>/autopilot.yaml) vencem os defaults.
        ov = load_project_overrides(proj["path"])
        phases = ov.get("phases") or (safe_phases() if body.safe else default_phases())
        models = ov.get("models") or {}
        cfg = config_for_project(
            proj["path"],
            sprint_status_path=info["sprint_status_path"],
            planning_artifacts_dir=info["planning_artifacts"],
            invoke_template=ov.get("invoke_template"),
            worker_model=models.get("worker"),
            advisor_model=models.get("advisor"),
            human_checkpoint=ov.get("human_checkpoint") or checkpoint,  # type: ignore[arg-type]
            phases=phases,
            advisor_prompt=ov.get("advisor_prompt"),
            recovery_policy=ov.get("recovery_policy"),
            enable_gate=ov.get("enable_gate"),
            auto_retrospective=ov.get("auto_retrospective"),
        )
        if body.fresh:   # "começar do zero": descarta sessão(ões) retomável(is)
            if body.scope == "epic":
                resume_mod.clear_all(cfg)
            else:
                resume_mod.clear_marker(cfg, body.id)
        mgr.start(cfg, body.scope, body.id, body.dry_run)
        return {"ok": True, "current": mgr.current}

    @app.post("/control")
    async def control(body: ControlRequest) -> dict[str, bool]:
        await mgr.control_action(body.action)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket) -> None:
        await socket.accept()
        queue = mgr.sink.subscribe()
        mgr.ws_connect()
        try:
            # snapshot do run atual antes do streaming
            for evt in mgr.sink.history:
                await socket.send_json(evt.to_dict())
            while True:
                evt = await queue.get()
                await socket.send_json(evt.to_dict())
        except WebSocketDisconnect:
            pass
        finally:
            mgr.sink.unsubscribe(queue)
            mgr.ws_disconnect()

    return app


def serve(host: str = "127.0.0.1", port: int = 8765) -> None:
    import socket

    import uvicorn

    # Idempotente: se a porta já está ocupada, verifica se é um backend nosso.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        if s.connect_ex((host, port)) == 0:
            try:
                import httpx

                if httpx.get(f"http://{host}:{port}/health", timeout=2).status_code == 200:
                    print(f"backend já está rodando em {host}:{port} — nada a fazer.")
                    return
            except Exception:
                pass
            print(f"porta {port} já está em uso por outro processo. "
                  f"Use --port para escolher outra, ou encerre o processo atual "
                  f"(pkill -f 'autopilot serve').")
            return

    uvicorn.run(create_app(), host=host, port=port, log_level="info")
