"""Detecção de um install BMAD v6 num projeto existente.

Lê `_bmad/bmm/config.yaml` (quando presente) para descobrir os diretórios de
artefatos, localiza o `sprint-status.yaml` e devolve uma sugestão de config +
o estado atual (epics/stories) — para o app retomar de qualquer ponto, nunca
assumindo projeto novo.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .status import SprintStatus, parse_story_key

_DEFAULT_IMPL = "_bmad-output/implementation-artifacts"
_DEFAULT_PLAN = "_bmad-output/planning-artifacts"


def _relativize(value: str, project_dir: Path) -> str:
    """Normaliza um path do config.yaml para relativo ao projeto."""
    v = (value or "").strip().replace("{project-root}", "").replace("{project_root}", "")
    v = v.lstrip("/").removeprefix("./")
    p = Path(value).expanduser()
    if p.is_absolute():
        try:
            return str(p.relative_to(project_dir))
        except ValueError:
            return v or _DEFAULT_IMPL
    return v


def _find_sprint_status(project_dir: Path, impl_rel: str) -> str | None:
    candidate = project_dir / impl_rel / "sprint-status.yaml"
    if candidate.exists():
        return str(candidate.relative_to(project_dir))
    # fallback: procura em profundidade limitada
    for path in project_dir.glob("**/sprint-status.yaml"):
        if any(part in (".venv", "node_modules", ".git") for part in path.parts):
            continue
        return str(path.relative_to(project_dir))
    return None


def detect(project_dir: str | Path) -> dict[str, Any]:
    project_dir = Path(project_dir).expanduser()
    out: dict[str, Any] = {
        "project_dir": str(project_dir),
        "exists": project_dir.is_dir(),
        "bmad_installed": (project_dir / "_bmad").is_dir(),
        "implementation_artifacts": _DEFAULT_IMPL,
        "planning_artifacts": _DEFAULT_PLAN,
        "sprint_status_path": None,
        "sprint_status_exists": False,
        "epics": [],
        "warnings": [],
    }
    if not out["exists"]:
        out["warnings"].append("diretório não existe")
        return out

    cfg_path = project_dir / "_bmad" / "bmm" / "config.yaml"
    if cfg_path.exists():
        try:
            data = yaml.safe_load(cfg_path.read_text()) or {}
            if data.get("implementation_artifacts"):
                out["implementation_artifacts"] = _relativize(
                    data["implementation_artifacts"], project_dir
                )
            if data.get("planning_artifacts"):
                out["planning_artifacts"] = _relativize(
                    data["planning_artifacts"], project_dir
                )
        except yaml.YAMLError as e:
            out["warnings"].append(f"config.yaml inválido: {e}")
    elif not out["bmad_installed"]:
        out["warnings"].append("BMAD não detectado (_bmad ausente)")

    ss_rel = _find_sprint_status(project_dir, out["implementation_artifacts"])
    out["sprint_status_path"] = ss_rel
    if ss_rel:
        out["sprint_status_exists"] = True
        out["epics"] = _read_epics(project_dir / ss_rel)
    else:
        out["warnings"].append("sprint-status.yaml não encontrado")
    return out


def _read_epics(sprint_status_file: Path) -> list[dict[str, Any]]:
    """Agrupa o development_status por epic, com as stories e seus status."""
    try:
        ss = SprintStatus(sprint_status_file)
        dev = ss.development_status()
    except Exception:
        return []

    by_epic: dict[int, dict[str, Any]] = {}
    for key, st in dev.items():
        story = parse_story_key(key)
        if story is None:
            continue
        epic = by_epic.setdefault(story.epic, {"epic": story.epic, "stories": []})
        epic["stories"].append({"key": key, "num": story.num, "status": st})

    # status do marcador epic-{n}, retrospectiva e flags de "runnable" (ordem)
    epics: list[dict[str, Any]] = []
    for epic_num in sorted(by_epic):
        e = by_epic[epic_num]
        e["stories"].sort(key=lambda s: s["num"])
        e["epic_status"] = dev.get(f"epic-{epic_num}")
        e["retrospective"] = dev.get(f"epic-{epic_num}-retrospective")
        e["runnable"], e["runnable_reason"] = ss.epic_runnable(epic_num)
        for s in e["stories"]:
            s["runnable"], s["runnable_reason"] = ss.story_runnable(s["key"])
        epics.append(e)
    return epics
