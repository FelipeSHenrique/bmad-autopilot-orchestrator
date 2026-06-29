"""Leitura/escrita do sprint-status.yaml do BMAD v6.

A chave raiz é `development_status`, um mapa de:
  - stories:        {epic}-{n}-{slug}        (ex.: 7-2-create-api)
  - marcadores epic: epic-{n}                 (ex.: epic-7)
  - retrospective:  epic-{n}-retrospective    (optional -> done)

A escrita preserva comentários/formatação via ruamel.yaml e é atômica
(escreve em .tmp e renomeia), espelhando internal/status/writer.go.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from ruamel.yaml import YAML

from . import router

_STORY_RE = re.compile(r"^(\d+)-(\d+)-(.+)$")
_EPIC_MARKER_RE = re.compile(r"^epic-(\d+)$")
_RETRO_RE = re.compile(r"^epic-(\d+)-retrospective$")

_DEV_KEY = "development_status"


@dataclass(frozen=True)
class Story:
    key: str          # ex.: 7-2-create-api
    epic: int
    num: int
    slug: str


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096  # evita rewraps que poluiriam o diff
    return y


def parse_story_key(key: str) -> Story | None:
    """Parseia uma chave de story; retorna None se for marcador de epic/retro."""
    if _EPIC_MARKER_RE.match(key) or _RETRO_RE.match(key):
        return None
    m = _STORY_RE.match(key)
    if not m:
        return None
    return Story(key=key, epic=int(m.group(1)), num=int(m.group(2)), slug=m.group(3))


class SprintStatus:
    """Acesso ao sprint-status.yaml de um projeto BMAD."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._yaml = _yaml()

    # ---- leitura -------------------------------------------------------
    def _load(self):
        if not self.path.exists():
            raise FileNotFoundError(f"sprint-status não encontrado: {self.path}")
        with self.path.open("r", encoding="utf-8") as fh:
            data = self._yaml.load(fh)
        if data is None or _DEV_KEY not in data:
            raise ValueError(f"{self.path}: chave '{_DEV_KEY}' ausente")
        return data

    def development_status(self) -> dict[str, str]:
        return dict(self._load()[_DEV_KEY])

    def story_status(self, key: str) -> str | None:
        return self.development_status().get(key)

    def stories(self) -> list[Story]:
        """Todas as stories (exclui marcadores epic-N e retrospectivas), sem ordem."""
        out: list[Story] = []
        for key in self.development_status():
            s = parse_story_key(key)
            if s is not None:
                out.append(s)
        return out

    def epic_stories(self, epic: int | str) -> list[Story]:
        """Stories de uma epic, ordenadas numericamente pelo número da story."""
        epic = int(epic)
        sel = [s for s in self.stories() if s.epic == epic]
        sel.sort(key=lambda s: s.num)
        return sel

    def epic_complete(self, epic: int | str) -> bool:
        """True se a epic tem stories e todas estão done."""
        sel = self.epic_stories(epic)
        return bool(sel) and all(
            self.story_status(s.key) == router.DONE for s in sel
        )

    def retrospective_key(self, epic: int | str) -> str:
        return f"epic-{int(epic)}-retrospective"

    def epic_key(self, epic: int | str) -> str:
        return f"epic-{int(epic)}"

    # ---- ordenação (bloquear play fora de ordem) -----------------------
    def story_runnable(self, key: str) -> tuple[bool, str]:
        """Uma story só roda se todas as stories de número MENOR na mesma epic
        estão done, e se ela própria ainda não está done."""
        story = parse_story_key(key)
        if story is None:
            return False, f"'{key}' não é uma story válida"
        if self.story_status(key) == router.DONE:
            return False, f"{key} já está done"
        earlier = [s for s in self.epic_stories(story.epic) if s.num < story.num]
        pendentes = [s.key for s in earlier if self.story_status(s.key) != router.DONE]
        if pendentes:
            return False, f"conclua antes: {', '.join(pendentes)}"
        return True, ""

    def epic_runnable(self, epic: int | str) -> tuple[bool, str]:
        """Uma epic é runnable se as epics anteriores estão completas E ainda há
        o que fazer: stories pendentes OU a retrospective (epic completa mas
        retrospective ainda não 'done')."""
        epic = int(epic)
        epics = sorted({s.epic for s in self.stories()})
        anteriores = [e for e in epics if e < epic and not self.epic_complete(e)]
        if anteriores:
            return False, f"conclua antes as epics: {anteriores}"
        # epic totalmente finalizada = stories done E retrospective done -> nada a fazer
        retro = self.story_status(self.retrospective_key(epic))
        if self.epic_complete(epic) and retro == "done":
            return False, f"epic {epic} já concluída (incl. retrospective)"
        return True, ""

    def next_phase(self, key: str) -> router.Phase | None:
        status = self.story_status(key)
        if status is None:
            raise KeyError(f"story '{key}' não está no sprint-status")
        return router.next_phase(status)

    # ---- escrita (atômica, preserva comentários) -----------------------
    def set_status(self, key: str, status: str) -> None:
        data = self._load()
        dev = data[_DEV_KEY]
        if key not in dev:
            raise KeyError(f"chave '{key}' não existe em {_DEV_KEY}")
        dev[key] = status
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            self._yaml.dump(data, fh)
        os.replace(tmp, self.path)  # rename atômico em POSIX
