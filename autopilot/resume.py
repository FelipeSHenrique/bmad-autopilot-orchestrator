"""Marcadores de resume de sessão (continuar uma fase do ponto exato).

Cada fase do worker fixa um `session_id` (UUID) e grava aqui qual sessão do
Claude está em andamento para um `target_id` (story_key, ou str(epic_id) na
retrospective). Se um re-run encontrar o marcador da fase interrompida (dentro
do TTL), o worker reabre a sessão com `ClaudeAgentOptions(resume=...)` em vez de
invocar a skill do zero. O marcador é removido quando a fase conclui.

Sem banco: um único JSON por projeto (`<project>/.autopilot/resume.json`), com
escrita atômica (.tmp + os.replace), no mesmo padrão de status.py.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Config


def _path(cfg: Config) -> Path:
    return cfg.bmad_project_dir / ".autopilot" / "resume.json"


def _load(cfg: Config) -> dict[str, Any]:
    p = _path(cfg)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _save(cfg: Config, data: dict[str, Any]) -> None:
    p = _path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, p)


def set_marker(cfg: Config, target_id: str, skill: str, session_id: str) -> None:
    """Registra a sessão em andamento para (target_id, skill)."""
    data = _load(cfg)
    data[target_id] = {
        "skill": skill,
        "session_id": session_id,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save(cfg, data)


def marker_for(cfg: Config, target_id: str, skill: str, ttl_hours: int) -> str | None:
    """Devolve o session_id retomável p/ (target_id, skill) se existir e estiver
    dentro do TTL; senão None (e limpa marcadores expirados/divergentes)."""
    data = _load(cfg)
    entry = data.get(target_id)
    if not isinstance(entry, dict) or entry.get("skill") != skill:
        return None
    sid = entry.get("session_id")
    if not sid:
        return None
    ts = entry.get("ts")
    if ts:
        try:
            age = datetime.now(timezone.utc) - datetime.fromisoformat(ts)
            if age.total_seconds() > ttl_hours * 3600:
                clear_marker(cfg, target_id)   # expirado -> começa do zero
                return None
        except ValueError:
            return None
    return sid


def clear_marker(cfg: Config, target_id: str) -> None:
    """Remove o marcador de um target (fase concluída ou 'começar do zero')."""
    data = _load(cfg)
    if target_id in data:
        del data[target_id]
        _save(cfg, data)


def clear_all(cfg: Config) -> None:
    """Remove todos os marcadores (ex.: 'começar do zero' numa epic inteira)."""
    p = _path(cfg)
    if p.exists():
        try:
            p.unlink()
        except OSError:
            pass


def available_targets(cfg: Config, ttl_hours: int) -> set[str]:
    """Conjunto de target_ids com sessão retomável dentro do TTL (p/ o /detect)."""
    data = _load(cfg)
    out: set[str] = set()
    now = datetime.now(timezone.utc)
    for target, entry in data.items():
        if not isinstance(entry, dict):
            continue
        ts = entry.get("ts")
        if not ts:
            continue
        try:
            if (now - datetime.fromisoformat(ts)).total_seconds() <= ttl_hours * 3600:
                out.add(target)
        except ValueError:
            continue
    return out
