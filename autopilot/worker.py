"""A sessão 'worker': roda UMA skill do BMAD numa sessão Claude nova.

Detecta toda decisão que a skill levanta e a roteia para o advisor:
  - estruturado: a skill chama AskUserQuestion -> can_use_tool -> advisor.
  - rede de segurança: a skill emite <ask> em texto e encerra o turno ->
    detectamos a pergunta -> advisor.decide_text -> injetamos via query().

Emite eventos (deltas token-a-token, tool_use) no EventSink e respeita o
RunControl (pause/stop). Cada fase usa uma ClaudeSDKClient própria.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any, Callable

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)

from . import events as ev
from .advisor import Advisor, _delta_text
from .config import Config, invoke_string
from .events import EventSink, RunControl

ROLE = "worker"

WORKER_SYS = """\
Você roda dentro de um harness automatizado, SEM humano disponível para \
responder. Ao executar um workflow do BMAD:

- Sempre que um passo contiver uma tag <ask> ou exigir uma decisão/escolha \
entre opções, CHAME a tool AskUserQuestion com as opções que o workflow lista. \
Não responda à pergunta sozinho e não assuma um default silenciosamente.
- Nunca pergunte em texto livre esperando que o usuário responda no chat; \
use sempre a tool AskUserQuestion para qualquer decisão.
- Quando a fase do workflow terminar, encerre normalmente (sem fazer uma \
pergunta final).
"""

# Dicas de que o worker está pedindo uma decisão em texto (PT + EN), incluindo
# marcadores explícitos de pausa (HALT) e prompts de confirmação que NÃO terminam
# em "?" — ex.: "(Y to continue)", "Próximo passo?", "aguardando sua escolha".
_QUESTION_HINTS = re.compile(
    r"(<ask\b"
    r"|choose option|choose \[|select (one|an option|from)|which (option|one)"
    r"|\(yes/no\)|\(y/n\)|\(y\b|\[y\]|\[q\]|q\] to quit|press enter"
    r"|please (choose|select|specify|provide)"
    r"|shall i\b|do you want|would you like|how would you like"
    r"|proceed\?|continue\?|next step\?"
    r"|\bHALT\b|awaiting your (choice|selection|input|response)"
    # português
    r"|pr[oó]ximo passo|aguardando (sua |a sua )?(escolha|sele[cç][aã]o|resposta|decis[aã]o)"
    r"|qual (op[cç][aã]o|a op[cç][aã]o|delas|voc[eê] (prefere|deseja|quer))"
    r"|escolha (uma|a op[cç][aã]o|entre)|deseja (continuar|prosseguir))",
    re.IGNORECASE,
)

# Linha de menu numerado: "1. …", "2) …", "❯ 1. …", "▎ 3. …".
_MENU_LINE = re.compile(r"^[\s>❯▎*+\-]*\(?[1-9][0-9]?\)?[.)]\s+\S")


def _looks_like_question(text: str) -> bool:
    """Rede de segurança: o worker pausou pedindo uma decisão em TEXTO?

    Só importa quando a skill NÃO chama AskUserQuestion (caminho estruturado).
    Combina dicas conhecidas (PT+EN, incl. HALT/menus) com '?' nas últimas linhas
    e um menu numerado com '?' por perto. Um falso positivo custa no máximo uma
    consulta ao advisor (limitada por max_decisions_per_phase)."""
    if not text or not text.strip():
        return False
    if _QUESTION_HINTS.search(text):
        return True
    recent = [ln.strip() for ln in text.strip().splitlines() if ln.strip()][-6:]
    if any(ln.endswith("?") for ln in recent):
        return True
    # menu numerado próximo do fim, com um '?' por perto — evita confundir com um
    # changelog "1. … 2. …" de fim de review (esse não tem '?' nem palavra-dica).
    menu = sum(1 for ln in recent if _MENU_LINE.match(ln))
    return menu >= 2 and any("?" in ln for ln in recent)


async def _dummy_pre_tool_hook(input_data: dict, tool_use_id: str | None, context: Any):
    # Mantém o stream aberto para o can_use_tool ser invocado (workaround Python).
    return {"continue_": True}


async def run_phase(
    skill: str,
    target_id: str,
    cfg: Config,
    advisor: Advisor | None,
    sink: EventSink,
    control: RunControl,
    *,
    dry_run: bool = False,
    done_predicate: Callable[[], bool] | None = None,
) -> None:
    """Roda uma fase (uma skill) até concluir, respondendo decisões via advisor.

    Conclusão é detectada de forma autoritativa por `done_predicate` (status no
    sprint-status.yaml). Um teto de decisões por fase evita loops em skills
    tagarelas/interativas (ex.: retrospective)."""
    await sink.emit(ev.phase_started(skill, target_id))
    invocation = invoke_string(cfg, skill, target_id)

    if dry_run:
        await sink.emit(ev.log(f"[dry-run] worker rodaria: {invocation!r}"))
        await sink.emit(ev.phase_ended(skill, target_id))
        return

    if advisor is not None:
        advisor.current_phase = skill  # decisões/memória ficam atribuídas a esta fase

    cap = cfg.max_decisions_per_phase
    decisions = {"n": 0}  # conta perguntas estruturadas + injeções de texto

    async def can_use_tool(
        tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        if tool_name == "AskUserQuestion":
            decisions["n"] += 1
            if decisions["n"] > cap:
                # anti-loop: chega de perguntas — manda finalizar a fase.
                return PermissionResultDeny(
                    message=("Já respondi muitas decisões nesta fase. Finalize agora: "
                             "conclua o workflow e atualize o sprint-status, sem mais perguntas.")
                )
            answers = await advisor.decide_structured(input_data.get("questions", []))
            return PermissionResultAllow(
                updated_input={
                    "questions": input_data.get("questions", []),
                    "answers": answers,
                }
            )
        return PermissionResultAllow(updated_input=input_data)

    options = ClaudeAgentOptions(
        cwd=str(cfg.bmad_project_dir),
        system_prompt=WORKER_SYS,
        permission_mode="acceptEdits",
        setting_sources=["user", "project", "local"],  # carrega .claude/ (skills BMAD)
        skills="all",
        include_partial_messages=True,
        can_use_tool=can_use_tool,
        hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[_dummy_pre_tool_hook])]},
        model=cfg.models.worker,
    )

    client = ClaudeSDKClient(options=options)
    await client.connect()
    try:
        await client.query(invocation)
        nudged = False  # (B) já demos um empurrão neste impasse?
        for _ in range(cfg.max_turns_per_phase):
            await control.gate()  # respeita pause; levanta StopRequested se stop
            last_text = await _drain_turn(client, sink)

            # 1) conclusão autoritativa por status -> fase concluída.
            if done_predicate is not None and done_predicate():
                break

            # 2) (A) pergunta/decisão detectada em texto -> advisor responde e injeta.
            if _looks_like_question(last_text):
                if decisions["n"] >= cap:  # anti-loop (skills tagarelas)
                    await client.query(
                        "Já respondi muitas decisões nesta fase; finalize agora e "
                        "atualize o sprint-status, sem mais perguntas.")
                    break
                decisions["n"] += 1
                answer = await advisor.decide_text(last_text)
                await control.gate()
                await client.query(answer)
                nudged = False  # houve interação -> reseta o empurrão
                continue

            # 3) (B) não-done e SEM pergunta clara: não encerrar em silêncio (senão o
            # orquestrador avançaria o status por cima de uma decisão não respondida).
            # Dá UM empurrão (auto-decidir/finalizar). Se nada mudar, conclui (break) e
            # o orquestrador grava o status — sem ficar consultando o advisor em loop.
            if last_text.strip() and not nudged:
                nudged = True
                await control.gate()
                await client.query(
                    "Se você está aguardando uma decisão ou exibindo opções, escolha a "
                    "melhor opção para ESTE projeto e prossiga. Se a fase terminou, "
                    "finalize e atualize o sprint-status. Não faça perguntas em texto "
                    "livre — use a tool AskUserQuestion.")
                continue
            break
        else:
            await sink.emit(
                ev.error(f"max_turns_per_phase atingido em {skill}:{target_id}")
            )
    except (asyncio.CancelledError, Exception):
        # Stop/erro: interrompe pedidos pendentes (evita "stream closed" no claude).
        try:
            await client.interrupt()
        except Exception:
            pass
        raise
    finally:
        try:
            await client.disconnect()
        except Exception:
            pass

    await sink.emit(ev.phase_ended(skill, target_id))


async def _drain_turn(client: ClaudeSDKClient, sink: EventSink) -> str:
    """Consome um ciclo de resposta, emitindo deltas/tool_use; devolve o texto."""
    parts: list[str] = []
    async for msg in client.receive_response():
        if isinstance(msg, StreamEvent):
            text = _delta_text(msg)
            if text:
                await sink.emit(ev.assistant_delta(ROLE, text))
        elif isinstance(msg, AssistantMessage):
            for block in msg.content:
                if isinstance(block, TextBlock):
                    parts.append(block.text)
                elif isinstance(block, ToolUseBlock):
                    await sink.emit(ev.tool_use(ROLE, block.name, block.input))
        elif isinstance(msg, ResultMessage):
            break
    return "".join(parts)
