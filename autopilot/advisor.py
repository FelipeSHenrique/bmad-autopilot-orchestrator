"""A sessão 'advisor': um agente Claude com persona que toma as decisões
que as skills do BMAD levantariam.

É read-only (Read/Grep/Glob): inspeciona o código e os artefatos de
planejamento do projeto antes de escolher, e devolve a escolha + uma
justificativa. Emite eventos (deltas token-a-token, tool_use, decisão) no
EventSink, e registra cada decisão num log de auditoria.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    StreamEvent,
    TextBlock,
    ToolUseBlock,
)

from . import events as ev
from .config import DEFAULT_ADVISOR_PROMPT, RECOVERY_SKILLS, Config
from .events import ConnectionLost, Escalation, EventSink, TokenLimitReached

try:  # erro de conexão do CLI do Claude (rede caiu / não alcançou a API)
    from claude_agent_sdk import CLIConnectionError
except ImportError:  # pragma: no cover
    CLIConnectionError = ()  # type: ignore[assignment]

# Padrões de mensagem que indicam falha de REDE (não rate-limit/cota).
_NET_PATTERNS = re.compile(
    r"network|connection (refused|reset|error|closed)|econnrefused|econnreset|"
    r"enotfound|etimedout|timed?\s*out|fetch failed|getaddrinfo|offline|"
    r"could not (connect|reach)|dns|unreachable|name resolution",
    re.IGNORECASE,
)


def is_network_error(exc: object) -> bool:
    """Heurística: a exceção parece falha de rede/conexão (transitória)?"""
    if CLIConnectionError and isinstance(exc, CLIConnectionError):
        return True
    return bool(_NET_PATTERNS.search(str(exc)))

ROLE = "advisor"
PERSONA = DEFAULT_ADVISOR_PROMPT  # default; pode ser sobrescrito por projeto

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_json(text: str) -> dict[str, Any] | None:
    candidates = list(_JSON_BLOCK_RE.findall(text))
    if not candidates:
        start, end = text.rfind("{"), text.rfind("}")
        if start != -1 and end > start:
            candidates.append(text[start : end + 1])
    for raw in reversed(candidates):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            continue
    return None


class Advisor:
    """Mantém uma sessão Claude por fase; cada decide() reaproveita o contexto."""

    def __init__(self, cfg: Config, sink: EventSink):
        self.cfg = cfg
        self.sink = sink
        self._client: ClaudeSDKClient | None = None
        self._log_path = cfg.bmad_project_dir / cfg.log_dir / "decisions.jsonl"
        self._memory_path = cfg.bmad_project_dir / ".autopilot" / "advisor-memory.md"
        self.current_phase = ""   # setado pelo worker antes de cada fase
        self.last_escalation: Escalation | None = None  # set se o advisor pediu recuperação

    def _memory_text(self, limit: int = 6000) -> str:
        """Conteúdo recente da memória do advisor (para dar contexto)."""
        if not self._memory_path.exists():
            return ""
        text = self._memory_path.read_text(encoding="utf-8")
        return text[-limit:]

    def _memory_context(self) -> str:
        mem = self._memory_text()
        if not mem.strip():
            return ""
        return (
            "\n\nMEMÓRIA (decisões anteriores suas neste projeto — use para "
            "manter consistência):\n----\n" + mem + "\n----\n"
        )

    async def __aenter__(self) -> "Advisor":
        options = ClaudeAgentOptions(
            cwd=str(self.cfg.bmad_project_dir),
            system_prompt=self.cfg.effective_advisor_prompt,
            tools=["Read", "Grep", "Glob"],   # read-only
            permission_mode="bypassPermissions",
            include_partial_messages=True,
            model=self.cfg.models.advisor,
        )
        self._client = ClaudeSDKClient(options=options)
        await self._client.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.disconnect()
            self._client = None

    async def _ask(self, prompt: str) -> str:
        assert self._client is not None, "Advisor não conectado (use 'async with')"
        await self._client.query(prompt)
        parts: list[str] = []
        async for msg in self._client.receive_response():
            await _raise_if_rate_limited(msg, self.sink)
            if isinstance(msg, StreamEvent):
                text = _delta_text(msg)
                if text:
                    await self.sink.emit(ev.assistant_delta(ROLE, text))
            elif isinstance(msg, AssistantMessage):
                for block in msg.content:
                    if isinstance(block, TextBlock):
                        parts.append(block.text)
                    elif isinstance(block, ToolUseBlock):
                        await self.sink.emit(ev.tool_use(ROLE, block.name, block.input))
            elif isinstance(msg, ResultMessage):
                break
        return "".join(parts)

    # ---- API pública ---------------------------------------------------
    async def decide_structured(self, questions: list[dict[str, Any]]) -> dict[str, Any]:
        prompt = (
            "O workflow BMAD apresentou as perguntas/escolhas abaixo (formato "
            "AskUserQuestion). Decida a melhor opção para cada uma.\n\n"
            f"```json\n{json.dumps(questions, ensure_ascii=False, indent=2)}\n```\n\n"
            "Responda APENAS com um bloco JSON:\n"
            '```json\n{\n  "answers": { "<texto exato da pergunta>": "<label '
            'escolhida>" },\n  "rationale": "<por que, citando código/artefatos>",\n'
            '  "escalate": { "skill": "bmad-quick-dev|bmad-correct-course", '
            '"reason": "<por que>" }   // OPCIONAL — omita se não precisar\n}\n```\n'
            "Para perguntas multiSelect, o valor é uma lista de labels."
            + self._memory_context()
        )
        text = await self._ask(prompt)
        data = _extract_json(text) or {}
        answers = data.get("answers", {})
        self._capture_escalation(data)
        await self._record(questions, answers, data.get("rationale", ""))
        return answers

    async def decide_text(self, question_text: str) -> str:
        prompt = (
            "O workflow BMAD pausou e fez a pergunta abaixo (texto livre, "
            "tipicamente uma tag <ask> com opções). Decida e produza a resposta "
            "EXATA que um usuário digitaria para o workflow seguir.\n\n"
            f"--- pergunta do worker ---\n{question_text}\n--- fim ---\n\n"
            "Responda APENAS com um bloco JSON:\n"
            '```json\n{\n  "answer": "<o que digitar para o worker>",\n'
            '  "rationale": "<por que, citando código/artefatos>",\n'
            '  "escalate": { "skill": "bmad-quick-dev|bmad-correct-course", '
            '"reason": "<por que>" }   // OPCIONAL — omita se não precisar\n}\n```'
            + self._memory_context()
        )
        text = await self._ask(prompt)
        data = _extract_json(text) or {}
        answer = str(data.get("answer", "")).strip()
        self._capture_escalation(data)
        await self._record(question_text, answer, data.get("rationale", ""))
        if not answer:
            answer = "Use your best judgment based on the existing codebase and proceed."
        return answer

    async def review_phase(self, skill: str, target_id: str, final_text: str) -> dict[str, Any]:
        """Gate de conclusão: a fase terminou — valida o resultado e diz se pode avançar.

        Inspeciona o diff/artefatos, as ACs e os ITENS DEFERIDOS (deferred-work.md),
        separando defer não-bloqueante de defer load-bearing. Devolve
        {ok, blockers, corrections}."""
        prompt = (
            f"A fase '{skill}' da story/epic '{target_id}' terminou. Resultado final do worker:\n"
            f"--- resultado ---\n{final_text}\n--- fim ---\n\n"
            "Valide se está tudo certo e se posso seguir para a próxima etapa. INSPECIONE o "
            "diff/artefatos, os critérios de aceite, E os itens deferidos (procure o arquivo "
            "deferred-work.md e a seção 'Defers' do resultado). Para CADA defer, julgue: é "
            "realmente NÃO-bloqueante para avançar, ou é load-bearing para a próxima fase? "
            "Um defer load-bearing (ou qualquer pendência que quebraria a próxima fase) entra "
            "em blockers. Responda APENAS com um bloco JSON:\n"
            '```json\n{\n  "ok": true,\n  "blockers": ["<pendência bloqueante, se houver>"],\n'
            '  "corrections": "<prompt direto do que o worker deve corrigir AGORA (vazio se ok)>"\n}\n```'
            + self._memory_context()
        )
        text = await self._ask(prompt)
        data = _extract_json(text) or {}
        ok = bool(data.get("ok", True))
        blockers = data.get("blockers") or []
        if not isinstance(blockers, list):
            blockers = [str(blockers)]
        corrections = str(data.get("corrections", "")).strip()
        return {"ok": ok and not blockers, "blockers": blockers, "corrections": corrections}

    def _capture_escalation(self, data: dict[str, Any]) -> None:
        """Lê o campo opcional 'escalate' do JSON do advisor e guarda em
        last_escalation. correct-course (plano) vence quick-dev (código) se ambos
        aparecerem na mesma fase."""
        esc = data.get("escalate")
        if not isinstance(esc, dict):
            return
        skill = str(esc.get("skill", "")).strip()
        if skill not in RECOVERY_SKILLS:
            return
        reason = str(esc.get("reason", "")).strip()
        cur = self.last_escalation
        if cur is None or (cur.skill != "bmad-correct-course"):
            self.last_escalation = Escalation(skill=skill, reason=reason)

    # ---- auditoria + memória -------------------------------------------
    async def _record(self, question: Any, decision: Any, rationale: str) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        await self.sink.emit(
            ev.advisor_decision(question, decision, rationale, phase=self.current_phase)
        )
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({
                "ts": ts, "phase": self.current_phase,
                "question": question, "decision": decision, "rationale": rationale,
            }, ensure_ascii=False) + "\n")
        self._append_memory(ts, question, decision, rationale)

    def _append_memory(self, ts: str, question: Any, decision: Any, rationale: str) -> None:
        """Anexa um item conciso à memória legível do advisor."""
        self._memory_path.parent.mkdir(parents=True, exist_ok=True)
        q = question if isinstance(question, str) else json.dumps(question, ensure_ascii=False)
        d = decision if isinstance(decision, str) else json.dumps(decision, ensure_ascii=False)
        entry = (
            f"\n## {ts} — {self.current_phase or 'fase?'}\n"
            f"- Pergunta: {q[:300]}\n- Escolha: {d[:300]}\n- Razão: {rationale[:400]}\n"
        )
        with self._memory_path.open("a", encoding="utf-8") as fh:
            fh.write(entry)


async def _raise_if_rate_limited(msg: Any, sink: EventSink) -> None:
    """Detecta limite de tokens/rate-limit no stream do SDK e encerra o run limpo.

    O SDK não lança exceção: o sinal vem (a) num evento com `rate_limit_info`
    cujo status é 'rejected', ou (b) num ResultMessage com is_error + HTTP 429/503/529.
    Emite token_limit e levanta TokenLimitReached (o loop encerra sem crashar)."""
    rl = getattr(msg, "rate_limit_info", None)
    if rl is not None and getattr(rl, "status", None) == "rejected":
        resets = getattr(rl, "resets_at", None)
        await sink.emit(ev.token_limit("limite de tokens atingido (rate limit)", resets))
        raise TokenLimitReached("rate limit", resets)
    if isinstance(msg, ResultMessage):
        status = getattr(msg, "api_error_status", None)
        if getattr(msg, "is_error", None) and status in (429, 503, 529):
            await sink.emit(ev.token_limit(f"erro da API {status} (limite/sobrecarga)", None))
            raise TokenLimitReached(f"api {status}")
        # 408/502/504 = timeout/gateway -> tratamos como queda de rede (retomável)
        if getattr(msg, "is_error", None) and status in (408, 502, 504):
            raise ConnectionLost(f"api {status} (rede/gateway)")


def _delta_text(stream_ev: StreamEvent) -> str:
    """Extrai texto incremental de um StreamEvent (content_block_delta)."""
    raw = stream_ev.event or {}
    if raw.get("type") == "content_block_delta":
        delta = raw.get("delta", {})
        if delta.get("type") == "text_delta":
            return delta.get("text", "")
    return ""
