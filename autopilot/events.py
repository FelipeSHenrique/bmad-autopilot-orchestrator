"""Barramento de eventos do autopilot.

O core emite `Event`s para um `EventSink`. A CLI registra um callback que
formata no terminal; o servidor registra uma fila por conexão WebSocket e
faz streaming. `RunControl` carrega os sinais de pause/stop/approve usados
pelos checkpoints e pelo controle vindo do app.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

# ---- Evento ------------------------------------------------------------


@dataclass
class Event:
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "ts": self.ts, **self.data}


@dataclass
class Escalation:
    """Pedido do advisor para rodar uma skill de recuperação (fora do lifecycle)."""

    skill: str            # "bmad-quick-dev" | "bmad-correct-course"
    reason: str = ""


# Construtores (mantêm os call-sites legíveis e o vocabulário consistente).
def run_started(scope: str, target: str, dry_run: bool) -> Event:
    return Event("run_started", {"scope": scope, "target": target, "dry_run": dry_run})


def run_ended(ok: bool, reason: str = "") -> Event:
    return Event("run_ended", {"ok": ok, "reason": reason})


def phase_started(skill: str, target: str) -> Event:
    return Event("phase_started", {"skill": skill, "target": target})


def phase_ended(skill: str, target: str) -> Event:
    return Event("phase_ended", {"skill": skill, "target": target})


def phase_resumed(skill: str, target: str) -> Event:
    return Event("phase_resumed", {"skill": skill, "target": target})


def assistant_delta(role: str, text: str) -> Event:
    # role: "worker" | "advisor"
    return Event("assistant_delta", {"role": role, "text": text})


def assistant_message(role: str, text: str) -> Event:
    return Event("assistant_message", {"role": role, "text": text})


def tool_use(role: str, name: str, tool_input: dict[str, Any]) -> Event:
    return Event("tool_use", {"role": role, "name": name, "input": tool_input})


def advisor_decision(question: Any, decision: Any, rationale: str, phase: str = "") -> Event:
    return Event(
        "advisor_decision",
        {"question": question, "decision": decision, "rationale": rationale, "phase": phase},
    )


def git_action(op: str, args: list[str], result: str = "") -> Event:
    return Event("git_action", {"op": op, "args": args, "result": result})


def status_changed(key: str, to: str, frm: str | None = None) -> Event:
    return Event("status_changed", {"key": key, "from": frm, "to": to})


def checkpoint_hit(label: str) -> Event:
    return Event("checkpoint_hit", {"label": label})


def recovery_recommended(skill: str, reason: str = "") -> Event:
    return Event("recovery_recommended", {"skill": skill, "reason": reason})


def recovery_started(skill: str, reason: str = "") -> Event:
    return Event("recovery_started", {"skill": skill, "reason": reason})


def run_paused(reason: str = "user") -> Event:
    return Event("run_paused", {"reason": reason})


def run_resumed() -> Event:
    return Event("run_resumed", {})


def token_limit(message: str, resets_at: int | None = None) -> Event:
    return Event("token_limit", {"message": message, "resets_at": resets_at})


def connection_lost(message: str = "") -> Event:
    return Event("connection_lost", {"message": message})


def log(message: str, level: str = "info") -> Event:
    return Event("log", {"message": message, "level": level})


def error(message: str) -> Event:
    return Event("error", {"message": message})


# ---- Sink --------------------------------------------------------------

Callback = Callable[[Event], None]


class EventSink:
    """Distribui eventos para callbacks síncronos (CLI) e filas async (WS)."""

    def __init__(self, history_limit: int = 5000):
        self._callbacks: list[Callback] = []
        self._queues: list[asyncio.Queue[Event]] = []
        self._history: list[Event] = []
        self._history_limit = history_limit

    def add_callback(self, cb: Callback) -> None:
        self._callbacks.append(cb)

    def subscribe(self) -> asyncio.Queue[Event]:
        q: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[Event]) -> None:
        if q in self._queues:
            self._queues.remove(q)

    @property
    def history(self) -> list[Event]:
        return list(self._history)

    def reset_history(self) -> None:
        self._history.clear()

    async def emit(self, ev: Event) -> None:
        self._history.append(ev)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        for cb in list(self._callbacks):
            try:
                cb(ev)
            except Exception:  # um subscriber ruim não derruba o run
                pass
        for q in list(self._queues):
            q.put_nowait(ev)


# ---- Controle do run ---------------------------------------------------


class StopRequested(Exception):
    """Levantada quando o usuário pede stop; o loop encerra graciosamente."""


class TokenLimitReached(Exception):
    """Levantada quando o limite de tokens/rate-limit é atingido no meio do run.
    O loop encerra de forma limpa (sem crash); o estado fica no sprint-status e o
    usuário retoma re-rodando."""

    def __init__(self, message: str, resets_at: int | None = None):
        super().__init__(message)
        self.resets_at = resets_at


class ConnectionLost(Exception):
    """Levantada quando a rede/conexão com o Claude cai no meio do run. Halt
    limpo (sem crash); o marcador de resume é preservado → retoma com ↻."""


class RunControl:
    """Sinais de controle do run ativo (pause/resume/stop/approve)."""

    def __init__(self, interactive_cli: bool = False):
        self._pause = asyncio.Event()
        self._pause.set()  # set = rodando; clear = pausado
        self._stop = asyncio.Event()
        self._approve = asyncio.Event()
        self._recovery_choice: str | None = None   # "run" | "skip"
        self.interactive_cli = interactive_cli

    # estado
    @property
    def stopped(self) -> bool:
        return self._stop.is_set()

    @property
    def paused(self) -> bool:
        return not self._pause.is_set()

    # comandos (vindos do app/CLI)
    def pause(self) -> None:
        self._pause.clear()

    def resume(self) -> None:
        self._pause.set()

    def stop(self) -> None:
        self._stop.set()
        self._pause.set()      # destrava quem estiver pausado
        self._approve.set()    # destrava quem aguarda checkpoint

    def approve(self) -> None:
        self._approve.set()

    def choose_recovery(self, action: str) -> None:
        """Resolve uma pausa de recuperação: 'run' (rodar a skill) | 'skip' (seguir)."""
        self._recovery_choice = action
        self._approve.set()

    # gates usados pelo loop
    def raise_if_stopped(self) -> None:
        if self._stop.is_set():
            raise StopRequested()

    async def gate(self) -> None:
        """Bloqueia enquanto pausado; levanta StopRequested se pedido stop."""
        self.raise_if_stopped()
        if not self._pause.is_set():
            await self._pause.wait()
        self.raise_if_stopped()

    async def wait_approval(self) -> None:
        """Aguarda um approve (checkpoint). Stop também libera."""
        self._approve.clear()
        await self._approve.wait()
        self.raise_if_stopped()

    async def wait_recovery_choice(self) -> str:
        """Aguarda a escolha humana numa pausa de recuperação. Stop levanta
        StopRequested; sem escolha explícita, assume 'skip' (conservador)."""
        self._recovery_choice = None
        self._approve.clear()
        await self._approve.wait()
        self.raise_if_stopped()
        return self._recovery_choice or "skip"


# Hook opcional para checkpoints interativos no modo CLI (input bloqueante).
CheckpointPrompt = Callable[[str], Awaitable[None]]
