"""Modelo e carregamento da configuração (autopilot.yaml)."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

# As skills do ciclo de implementação do BMAD v6, na ordem do lifecycle.
CREATE_STORY = "bmad-create-story"
DEV_STORY = "bmad-dev-story"
CODE_REVIEW = "bmad-code-review"
RETROSPECTIVE = "bmad-retrospective"

# Skills de RECUPERAÇÃO (fora do lifecycle) — disparadas por escalação do advisor.
QUICK_DEV = "bmad-quick-dev"            # correção de código contida
CORRECT_COURSE = "bmad-correct-course"  # correção de planejamento (reescreve plano)
RECOVERY_SKILLS = (QUICK_DEV, CORRECT_COURSE)

HumanCheckpoint = Literal["none", "end-of-story", "retrospective"]
# tiered: quick-dev autônomo, correct-course pausa | pause: ambos pausam | auto: ambos rodam
RecoveryPolicy = Literal["tiered", "pause", "auto"]

# Persona padrão do advisor (editável por projeto via autopilot.yaml).
DEFAULT_ADVISOR_PROMPT = """\
Você é o ARQUITETO/DECISOR técnico deste projeto, operando dentro de um \
harness automatizado que roda o ciclo de implementação do BMAD. As skills do \
BMAD (create-story, dev-story, code-review, retrospective) levantam decisões e \
escolhas; o seu papel é escolher, em nome da equipe, a melhor opção para a \
saúde de longo prazo do sistema.

Antes de decidir, INSPECIONE o que for relevante: leia o código existente \
(Read/Grep/Glob) e os artefatos de planejamento do BMAD (epics, PRD, \
arquitetura) sob o diretório do projeto. Prefira a opção mais alinhada aos \
padrões já existentes no repositório, à arquitetura definida e aos critérios \
de aceite da story/epic. Seja decisivo: nunca devolva "não sei" ou peça ajuda \
ao usuário — não há humano disponível. Quando faltar informação, escolha a \
opção mais conservadora e segura e explique o porquê.

ESCALAÇÃO (recuperação): se a fase atual do lifecycle NÃO consegue resolver a \
situação e o caminho certo é rodar uma skill de recuperação do BMAD, sinalize \
no campo opcional "escalate" do seu JSON:
- "bmad-quick-dev": quando há um BUG/ajuste de código focado que a fase atual \
não cobre (correção contida, sem mudar o plano).
- "bmad-correct-course": quando o problema é de PLANEJAMENTO/requisitos \
(precisa reescrever epics/PRD/arquitetura, adicionar/remover stories).
Só escale quando for realmente necessário; na dúvida, NÃO escale (omita o \
campo) e responda a decisão normalmente.
"""


@dataclass
class GitAction:
    """Uma ação de git a executar ao fim de uma fase.

    `op` é o nome da operação (create_branch, commit, open_pr, merge_pr) e
    `params` o valor associado no YAML (string ou dict).
    """

    op: str
    params: Any


@dataclass
class PhaseConfig:
    """Configuração de uma fase (uma skill do BMAD)."""

    name: str
    git: list[GitAction] = field(default_factory=list)


@dataclass
class Models:
    worker: str = "claude-opus-4-8"
    advisor: str = "claude-sonnet-5"


@dataclass
class Autonomy:
    human_checkpoint: HumanCheckpoint = "none"
    recovery_policy: RecoveryPolicy = "tiered"


@dataclass
class Config:
    bmad_project_dir: Path
    sprint_status_path: str = "_bmad-output/implementation-artifacts/sprint-status.yaml"
    planning_artifacts_dir: str = "_bmad-output/planning-artifacts"
    # Como invocar a skill via client.query(). {skill} e {story_id} são
    # substituídos. Os dois formatos comuns do v6 estão documentados no plano;
    # o default usa um prompt natural que o Claude Code resolve para a skill.
    invoke_template: str = "Run the {skill} workflow for {story_id}."
    models: Models = field(default_factory=Models)
    autonomy: Autonomy = field(default_factory=Autonomy)
    phases: dict[str, PhaseConfig] = field(default_factory=dict)
    log_dir: str = ".autopilot/logs"
    max_turns_per_phase: int = 40
    max_decisions_per_phase: int = 12   # anti-loop: teto de decisões/perguntas por fase
    max_recoveries_per_story: int = 2   # anti-loop: teto de recuperações por story
    max_phase_iters_per_story: int = 12 # anti-loop: teto de iterações de fase por story
    resume_ttl_hours: int = 24          # validade do marcador de resume de sessão
    status_poll_interval: float = 1.0   # poll do sprint-status p/ status ao vivo (incl. in-progress)
    enable_gate: bool = True            # advisor revisa o resultado de cada fase antes de avançar
    max_gate_rounds: int = 2            # rodadas de correção do gate antes de pausar pro humano
    auto_retrospective: bool = True     # ao concluir a última story da epic, já roda a retrospective
    advisor_prompt: str | None = None   # None => DEFAULT_ADVISOR_PROMPT

    @property
    def effective_advisor_prompt(self) -> str:
        return self.advisor_prompt or DEFAULT_ADVISOR_PROMPT

    @property
    def sprint_status_file(self) -> Path:
        return self.bmad_project_dir / self.sprint_status_path

    @property
    def planning_dir(self) -> Path:
        return self.bmad_project_dir / self.planning_artifacts_dir

    def phase(self, name: str) -> PhaseConfig:
        return self.phases.get(name, PhaseConfig(name=name))


def _parse_git(raw: list[Any] | None) -> list[GitAction]:
    actions: list[GitAction] = []
    for item in raw or []:
        if not isinstance(item, dict) or len(item) != 1:
            raise ValueError(
                f"Cada ação de git deve ser um dict de uma chave só, recebido: {item!r}"
            )
        (op, params), = item.items()
        actions.append(GitAction(op=op, params=params))
    return actions


def load_config(path: str | Path) -> Config:
    """Carrega e valida o autopilot.yaml."""
    path = Path(path)
    data: dict[str, Any] = yaml.safe_load(path.read_text()) or {}

    if "bmad_project_dir" not in data:
        raise ValueError("autopilot.yaml: 'bmad_project_dir' é obrigatório")
    bmad_dir = Path(data["bmad_project_dir"]).expanduser()

    models_raw = data.get("models", {}) or {}
    models = Models(
        worker=models_raw.get("worker", Models.worker),
        advisor=models_raw.get("advisor", Models.advisor),
    )

    autonomy_raw = data.get("autonomy", {}) or {}
    autonomy = Autonomy(
        human_checkpoint=autonomy_raw.get("human_checkpoint", "none"),
        recovery_policy=autonomy_raw.get("recovery_policy", "tiered"),
    )

    phases: dict[str, PhaseConfig] = {}
    for name, pdata in (data.get("phases", {}) or {}).items():
        pdata = pdata or {}
        phases[name] = PhaseConfig(name=name, git=_parse_git(pdata.get("git")))

    cfg = Config(
        bmad_project_dir=bmad_dir,
        sprint_status_path=data.get("sprint_status_path", Config.sprint_status_path),
        planning_artifacts_dir=data.get(
            "planning_artifacts_dir", Config.planning_artifacts_dir
        ),
        invoke_template=data.get("invoke_template", Config.invoke_template),
        models=models,
        autonomy=autonomy,
        phases=phases,
        log_dir=data.get("log_dir", Config.log_dir),
        max_turns_per_phase=int(data.get("max_turns_per_phase", Config.max_turns_per_phase)),
        enable_gate=bool(data.get("enable_gate", Config.enable_gate)),
        auto_retrospective=bool(data.get("auto_retrospective", Config.auto_retrospective)),
    )
    return cfg


def invoke_string(cfg: Config, skill: str, story_id: str) -> str:
    """Monta a string que dispara a skill via client.query()."""
    return cfg.invoke_template.format(skill=skill, story_id=story_id)


def default_phases() -> dict[str, PhaseConfig]:
    """Regras de git padrão por fase (mesma config do autopilot.example.yaml)."""
    return {
        CREATE_STORY: PhaseConfig(CREATE_STORY, [
            GitAction("create_branch", "story/{story_id}"),
            GitAction("commit", "chore: draft story {story_id}"),
        ]),
        DEV_STORY: PhaseConfig(DEV_STORY, [
            GitAction("commit", "feat: implement {story_id}"),
        ]),
        CODE_REVIEW: PhaseConfig(CODE_REVIEW, [
            GitAction("commit", "chore: review {story_id}"),
            GitAction("open_pr", {"base": "main", "title": "{story_id}"}),
            GitAction("merge_pr", {"method": "squash"}),
        ]),
        RETROSPECTIVE: PhaseConfig(RETROSPECTIVE, [
            GitAction("commit", "chore: retrospective epic-{epic_id}"),
        ]),
        QUICK_DEV: PhaseConfig(QUICK_DEV, [
            GitAction("commit", "fix: quick-dev {story_id}"),
        ]),
        CORRECT_COURSE: PhaseConfig(CORRECT_COURSE, [
            GitAction("commit", "chore: correct-course epic-{epic_id}"),
        ]),
    }


def safe_phases() -> dict[str, PhaseConfig]:
    """Regras de git SEGURAS para teste: branch dedicada + commits locais.
    Sem push, sem PR, sem merge — não toca na main nem no remoto."""
    return {
        CREATE_STORY: PhaseConfig(CREATE_STORY, [
            GitAction("create_branch", "autopilot/{story_id}"),
            GitAction("commit", "chore: draft story {story_id}"),
        ]),
        DEV_STORY: PhaseConfig(DEV_STORY, [
            GitAction("commit", "feat: implement {story_id}"),
        ]),
        CODE_REVIEW: PhaseConfig(CODE_REVIEW, [
            GitAction("commit", "chore: review {story_id}"),
        ]),
        RETROSPECTIVE: PhaseConfig(RETROSPECTIVE, [
            GitAction("commit", "chore: retrospective epic-{epic_id}"),
        ]),
        QUICK_DEV: PhaseConfig(QUICK_DEV, [
            GitAction("commit", "fix: quick-dev {story_id}"),
        ]),
        CORRECT_COURSE: PhaseConfig(CORRECT_COURSE, [
            GitAction("commit", "chore: correct-course epic-{epic_id}"),
        ]),
    }


def config_for_project(
    project_dir: str | Path,
    *,
    sprint_status_path: str | None = None,
    planning_artifacts_dir: str | None = None,
    invoke_template: str | None = None,
    worker_model: str | None = None,
    advisor_model: str | None = None,
    human_checkpoint: HumanCheckpoint | None = None,
    phases: dict[str, PhaseConfig] | None = None,
    advisor_prompt: str | None = None,
    recovery_policy: RecoveryPolicy | None = None,
    enable_gate: bool | None = None,
    auto_retrospective: bool | None = None,
) -> Config:
    """Constrói uma Config programaticamente (usada pelo backend/app)."""
    return Config(
        bmad_project_dir=Path(project_dir).expanduser(),
        sprint_status_path=sprint_status_path or Config.sprint_status_path,
        planning_artifacts_dir=planning_artifacts_dir or Config.planning_artifacts_dir,
        invoke_template=invoke_template or Config.invoke_template,
        models=Models(
            worker=worker_model or Models.worker,
            advisor=advisor_model or Models.advisor,
        ),
        autonomy=Autonomy(
            human_checkpoint=human_checkpoint or "none",
            recovery_policy=recovery_policy or "tiered",
        ),
        phases=phases if phases is not None else default_phases(),
        advisor_prompt=advisor_prompt,
        enable_gate=True if enable_gate is None else enable_gate,
        auto_retrospective=True if auto_retrospective is None else auto_retrospective,
    )


# ---- overrides por projeto (<project>/autopilot.yaml) ------------------

# Campos que o usuário pode customizar por projeto (no app ou no YAML).
def phases_to_dict(phases: dict[str, PhaseConfig]) -> dict[str, Any]:
    """Serializa as fases para a forma do autopilot.yaml (p/ o endpoint /config)."""
    out: dict[str, Any] = {}
    for name, pc in phases.items():
        out[name] = {"git": [{a.op: a.params} for a in pc.git]}
    return out


def project_overrides_path(project_dir: str | Path) -> Path:
    return Path(project_dir).expanduser() / "autopilot.yaml"


def load_project_overrides(project_dir: str | Path) -> dict[str, Any]:
    """Lê <project>/autopilot.yaml (se existir) e devolve só os campos
    customizáveis: advisor_prompt, phases, invoke_template, models, human_checkpoint."""
    p = project_overrides_path(project_dir)
    if not p.exists():
        return {}
    data: dict[str, Any] = yaml.safe_load(p.read_text()) or {}
    out: dict[str, Any] = {}
    if "advisor_prompt" in data:
        out["advisor_prompt"] = data["advisor_prompt"]
    if "invoke_template" in data:
        out["invoke_template"] = data["invoke_template"]
    if data.get("models"):
        out["models"] = {
            "worker": data["models"].get("worker"),
            "advisor": data["models"].get("advisor"),
        }
    if (data.get("autonomy") or {}).get("human_checkpoint"):
        out["human_checkpoint"] = data["autonomy"]["human_checkpoint"]
    elif data.get("human_checkpoint"):
        out["human_checkpoint"] = data["human_checkpoint"]
    if (data.get("autonomy") or {}).get("recovery_policy"):
        out["recovery_policy"] = data["autonomy"]["recovery_policy"]
    elif data.get("recovery_policy"):
        out["recovery_policy"] = data["recovery_policy"]
    if "enable_gate" in data:
        out["enable_gate"] = bool(data["enable_gate"])
    if "auto_retrospective" in data:
        out["auto_retrospective"] = bool(data["auto_retrospective"])
    if data.get("phases"):
        out["phases"] = {
            name: PhaseConfig(name=name, git=_parse_git((pd or {}).get("git")))
            for name, pd in data["phases"].items()
        }
    return out


def save_project_overrides(project_dir: str | Path, data: dict[str, Any]) -> None:
    """Grava o autopilot.yaml do projeto (apenas os campos customizáveis)."""
    p = project_overrides_path(project_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True))
