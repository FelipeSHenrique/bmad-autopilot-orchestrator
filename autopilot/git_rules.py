"""Aplica as regras de git por fase (branch, commit, PR, merge).

Ações determinísticas, rodadas pelo orquestrador entre as skills. Emite
eventos git_action no EventSink. Em dry-run não executa nada.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import events as ev
from .config import PhaseConfig
from .events import EventSink


@dataclass
class GitContext:
    story_id: str = ""
    epic_id: str = ""
    branch: str = ""
    base: str = "main"   # branch base do PR (setada no open_pr; usada no merge sync)

    def fmt(self, template: str) -> str:
        return template.format(
            story_id=self.story_id, epic_id=self.epic_id, branch=self.branch
        )


class GitRunner:
    def __init__(self, cwd: Path, dry_run: bool = False):
        self.cwd = Path(cwd)
        self.dry_run = dry_run

    def run(self, args: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
        if self.dry_run:
            return subprocess.CompletedProcess(args, 0, "[dry-run]", "")
        return subprocess.run(
            args, cwd=self.cwd, check=check, text=True, capture_output=True
        )

    def current_branch(self) -> str:
        if self.dry_run:
            return "<branch>"
        r = self.run(["git", "rev-parse", "--abbrev-ref", "HEAD"], check=False)
        return r.stdout.strip()

    # ---- operações (retornam um resumo) --------------------------------
    def create_branch(self, name: str, ctx: GitContext) -> str:
        ctx.branch = name
        if self.dry_run:
            return f"switch -c {name}"
        exists = self.run(["git", "rev-parse", "--verify", "--quiet", name], check=False)
        if exists.returncode == 0:
            self.run(["git", "switch", name])
            return f"switch {name}"
        self.run(["git", "switch", "-c", name])
        return f"switch -c {name}"

    def commit(self, message: str, ctx: GitContext) -> str:
        self.run(["git", "add", "-A"])
        if not self.dry_run:
            status = self.run(["git", "status", "--porcelain"], check=False)
            if not status.stdout.strip():
                return "nada para commitar"
        self.run(["git", "commit", "-m", message])
        return f"commit: {message}"

    def open_pr(self, base: str, title: str, ctx: GitContext) -> str:
        branch = ctx.branch or self.current_branch()
        ctx.branch = branch
        ctx.base = base
        self.run(["git", "push", "-u", "origin", branch])
        r = self.run(
            ["gh", "pr", "create", "--base", base, "--head", branch,
             "--title", title, "--body", title],
            check=False,
        )
        url = (r.stdout or "").strip().splitlines()[-1] if r.stdout else ""
        return f"PR aberto {url}".strip()

    def merge_pr(self, method: str, ctx: GitContext) -> str:
        branch = ctx.branch or self.current_branch()
        base = ctx.base or "main"
        flag = {"squash": "--squash", "merge": "--merge", "rebase": "--rebase"}.get(
            method, "--squash"
        )
        self.run(["gh", "pr", "merge", branch, flag, "--delete-branch"], check=False)
        # `gh pr merge` faz o merge no REMOTO e deixa o `base` LOCAL defasado. Sem
        # sincronizar, o orquestrador relê o sprint-status antigo (story volta a
        # backlog) e re-roda a story inteira em loop. Alinha o base local ao remoto.
        self.run(["git", "checkout", base], check=False)
        self.run(["git", "fetch", "origin", base], check=False)
        self.run(["git", "reset", "--hard", f"origin/{base}"], check=False)
        return f"merge {method} ({branch})"


def _args(params: Any, primary: str) -> dict[str, Any]:
    return params if isinstance(params, dict) else {primary: params}


async def apply_phase(
    phase: PhaseConfig, runner: GitRunner, ctx: GitContext, sink: EventSink
) -> GitContext:
    for action in phase.git:
        op, raw = action.op, action.params
        if op == "create_branch":
            p = _args(raw, "name")
            name = ctx.fmt(p["name"])
            summary = runner.create_branch(name, ctx)
            await sink.emit(ev.git_action(op, [name], summary))
        elif op == "commit":
            p = _args(raw, "message")
            msg = ctx.fmt(p["message"])
            summary = runner.commit(msg, ctx)
            await sink.emit(ev.git_action(op, [msg], summary))
        elif op == "open_pr":
            p = _args(raw, "title")
            base, title = p.get("base", "main"), ctx.fmt(p.get("title", ctx.story_id))
            summary = runner.open_pr(base, title, ctx)
            await sink.emit(ev.git_action(op, [base, title], summary))
        elif op == "merge_pr":
            p = _args(raw, "method")
            method = p.get("method", "squash")
            summary = runner.merge_pr(method, ctx)
            await sink.emit(ev.git_action(op, [method], summary))
        else:
            await sink.emit(ev.error(f"ação de git desconhecida: {op}"))
    return ctx
