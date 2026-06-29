"""Fake do ClaudeSDKClient para testes de integração worker → advisor.

Imita a superfície real usada por autopilot.worker e autopilot.advisor, sem
chamar o Claude (zero tokens). O `install(monkeypatch)` troca, nos dois módulos,
o `ClaudeSDKClient` e os tipos de mensagem usados nos `isinstance`.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from autopilot import advisor as advisor_mod
from autopilot import worker as worker_mod

# Pergunta de exemplo que o "worker" levanta via AskUserQuestion.
QUESTIONS: list[dict[str, Any]] = [
    {
        "question": "Qual abordagem para a camada de dados?",
        "header": "Dados",
        "options": [
            {"label": "Repository pattern", "description": "alinha com o existente"},
            {"label": "Active Record", "description": "mais simples"},
        ],
        "multiSelect": False,
    }
]


# ---- tipos fake (batem com os isinstance de worker/advisor) -------------
class FakeStreamEvent:
    def __init__(self, event: dict):
        self.event = event


class FakeTextBlock:
    def __init__(self, text: str):
        self.text = text


class FakeToolUseBlock:
    def __init__(self, name: str, input: dict):
        self.name = name
        self.input = input


class FakeAssistantMessage:
    def __init__(self, content: list):
        self.content = content


class FakeResultMessage:
    pass


def _delta(text: str) -> FakeStreamEvent:
    return FakeStreamEvent({"type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": text}})


class Recorder:
    def __init__(self) -> None:
        self.connects: list[str] = []      # "worker" | "advisor"
        self.disconnects: list[str] = []
        self.queries: list[tuple[str, str]] = []
        self.interrupts = 0
        self.answers: Any = None
        self.worker_mode = "ask"           # "ask" | "block" | "loop"
        self.advisor_escalate: str | None = None   # skill que o advisor pede (recuperação)
        self.advisor_escalate_once = True          # consome após a 1ª decisão


_REC: Recorder | None = None


class FakeClaudeSDKClient:
    def __init__(self, options: Any):
        self.options = options
        self.role = "worker" if getattr(options, "can_use_tool", None) is not None else "advisor"
        self._turn = 0

    async def connect(self) -> None:
        _REC.connects.append(self.role)

    async def disconnect(self) -> None:
        _REC.disconnects.append(self.role)

    async def interrupt(self) -> None:
        _REC.interrupts += 1

    async def query(self, prompt: str) -> None:
        self._turn += 1
        _REC.queries.append((self.role, prompt))

    async def receive_response(self):
        if self.role == "advisor":
            yield _delta("decidindo...")
            answer = {
                "answers": {QUESTIONS[0]["question"]: QUESTIONS[0]["options"][0]["label"]},
                "rationale": "Repository pattern alinhado ao padrão existente no repo.",
            }
            if _REC.advisor_escalate:
                answer["escalate"] = {"skill": _REC.advisor_escalate, "reason": "fake escalation"}
                if _REC.advisor_escalate_once:
                    _REC.advisor_escalate = None
            yield FakeAssistantMessage([FakeTextBlock("```json\n" + json.dumps(answer) + "\n```")])
            yield FakeResultMessage()
            return

        # worker
        if _REC.worker_mode == "block":
            yield _delta("trabalhando...")
            await asyncio.sleep(3600)        # bloqueia (cancelável) — simula turno longo
            yield FakeResultMessage()        # nunca alcançado
            return

        if _REC.worker_mode == "loop":
            # nunca conclui: termina todo turno com uma pergunta (skill tagarela).
            yield _delta("Trabalhando... ")
            yield FakeAssistantMessage([FakeTextBlock("Qual opção você prefere? [1] [2]")])
            yield FakeResultMessage()
            return

        # modo "ask": no 1º turno levanta a decisão via can_use_tool e conclui
        if self._turn == 1:
            yield _delta("analisando a story...")
            res = await self.options.can_use_tool("AskUserQuestion", {"questions": QUESTIONS}, object())
            _REC.answers = getattr(res, "updated_input", {}).get("answers")
            yield FakeAssistantMessage([FakeTextBlock("Apliquei a escolha do advisor e concluí.")])
            yield FakeResultMessage()
        else:
            yield FakeAssistantMessage([FakeTextBlock("Nada mais a fazer.")])
            yield FakeResultMessage()


def install(monkeypatch) -> Recorder:
    """Instala os fakes em worker e advisor; devolve o Recorder."""
    global _REC
    _REC = Recorder()
    for mod in (worker_mod, advisor_mod):
        monkeypatch.setattr(mod, "ClaudeSDKClient", FakeClaudeSDKClient)
        monkeypatch.setattr(mod, "StreamEvent", FakeStreamEvent)
        monkeypatch.setattr(mod, "AssistantMessage", FakeAssistantMessage)
        monkeypatch.setattr(mod, "TextBlock", FakeTextBlock)
        monkeypatch.setattr(mod, "ToolUseBlock", FakeToolUseBlock)
        monkeypatch.setattr(mod, "ResultMessage", FakeResultMessage)
    return _REC
