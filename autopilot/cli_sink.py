"""Subscriber de terminal: formata os eventos do EventSink na CLI.

Mantém a experiência de linha de comando enquanto o mesmo fluxo de eventos
alimenta o app via WebSocket.
"""

from __future__ import annotations

import sys

from .events import Event

# Estado para agrupar deltas de streaming por papel sem quebrar linha a cada token.
_last_role: dict[str, str] = {"v": ""}


def render(ev: Event) -> None:
    k = ev.kind
    d = ev.data

    if k == "assistant_delta":
        role = d.get("role", "")
        if _last_role["v"] != role:
            sys.stdout.write(f"\n[{role}] ")
            _last_role["v"] = role
        sys.stdout.write(d.get("text", ""))
        sys.stdout.flush()
        return

    # qualquer evento não-delta encerra o bloco de streaming corrente
    if _last_role["v"]:
        sys.stdout.write("\n")
        _last_role["v"] = ""

    if k == "run_started":
        print(f"▶ run {d['scope']}={d['target']}" + (" (dry-run)" if d.get("dry_run") else ""))
    elif k == "run_ended":
        print(f"■ run {'ok' if d.get('ok') else 'interrompido'}: {d.get('reason','')}".rstrip())
    elif k == "phase_started":
        print(f"\n=== fase {d['skill']} :: {d['target']} ===")
    elif k == "phase_ended":
        pass
    elif k == "tool_use":
        print(f"   · {d.get('role')} tool: {d.get('name')}")
    elif k == "advisor_decision":
        print(f"   ◆ advisor: {d.get('decision')}")
        if d.get("rationale"):
            r = d["rationale"]
            print(f"     razão: {r[:160]}{'…' if len(r) > 160 else ''}")
    elif k == "git_action":
        print(f"   ▸ git {d.get('op')}: {d.get('result','')}")
    elif k == "status_changed":
        frm = d.get("from")
        arrow = f"{frm} → " if frm else ""
        print(f"   status {d.get('key')}: {arrow}{d.get('to')}")
    elif k == "checkpoint_hit":
        print(f"⏸  checkpoint: {d.get('label')}")
    elif k == "log":
        print(f"   {d.get('message')}")
    elif k == "error":
        print(f"   ✖ {d.get('message')}", file=sys.stderr)
