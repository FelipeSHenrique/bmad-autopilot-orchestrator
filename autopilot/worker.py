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

_QUESTION_HINTS = re.compile(
    r"(<ask\b|choose option|\(yes/no\)|\[q\]|q\] to quit|select (one|an option)|"
    r"which (option|one)|please (choose|select|specify|provide))",
    re.IGNORECASE,
)


def _looks_like_question(text: str) -> bool:
    if not text:
        return False
    if _QUESTION_HINTS.search(text):
        return True
    tail = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    return bool(tail) and tail[-1].endswith("?")


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
        for _ in range(cfg.max_turns_per_phase):
            await control.gate()  # respeita pause; levanta StopRequested se stop
            last_text = await _drain_turn(client, sink)

            # 1) sinal autoritativo: o status já chegou ao alvo -> fase concluída.
            if done_predicate is not None and done_predicate():
                break

            # 2) pergunta pendente em texto -> advisor responde (rede de segurança).
            if _looks_like_question(last_text) and decisions["n"] <= cap:
                decisions["n"] += 1
                if decisions["n"] > cap:
                    await client.query(
                        "Já respondi muitas decisões; finalize a fase agora e atualize "
                        "o sprint-status, sem mais perguntas.")
                else:
                    answer = await advisor.decide_text(last_text)
                    await control.gate()
                    await client.query(answer)
                continue

            # 3) sem pergunta e sem done: se a fase escreve status, isso é a conclusão;
            #    senão, o loop encerra aqui de qualquer forma (o orquestrador grava o status).
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
