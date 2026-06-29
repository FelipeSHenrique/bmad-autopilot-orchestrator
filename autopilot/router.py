"""Roteamento status -> próxima skill (espelha internal/router do bmad_automated)."""

from __future__ import annotations

from dataclasses import dataclass

from .config import CODE_REVIEW, CREATE_STORY, DEV_STORY

# Valores de status de story do BMAD v6.
BACKLOG = "backlog"
READY_FOR_DEV = "ready-for-dev"
IN_PROGRESS = "in-progress"
REVIEW = "review"
DONE = "done"

# Status que indicam que a story terminou (não há próxima fase).
TERMINAL = {DONE}


@dataclass(frozen=True)
class Phase:
    """Uma fase do lifecycle: a skill a rodar e o status resultante."""

    skill: str
    next_status: str


# status atual -> (skill, status após a fase concluir)
_ROUTES: dict[str, Phase] = {
    BACKLOG: Phase(CREATE_STORY, READY_FOR_DEV),
    READY_FOR_DEV: Phase(DEV_STORY, REVIEW),
    IN_PROGRESS: Phase(DEV_STORY, REVIEW),
    REVIEW: Phase(CODE_REVIEW, DONE),
}


def next_phase(status: str) -> Phase | None:
    """Dada a situação atual da story, devolve a próxima fase ou None se done."""
    if status in TERMINAL:
        return None
    return _ROUTES.get(status)


def lifecycle(status: str) -> list[Phase]:
    """Sequência completa de fases do status atual até done."""
    steps: list[Phase] = []
    cur = status
    seen: set[str] = set()
    while (phase := next_phase(cur)) is not None:
        steps.append(phase)
        if phase.next_status in seen:  # proteção contra ciclo
            break
        seen.add(phase.next_status)
        cur = phase.next_status
    return steps
