"""CLI do autopilot.

Uso:
  autopilot run --story 7-2-create-api
  autopilot run --epic 7
  autopilot run --epic 7 --dry-run
  autopilot status
  autopilot serve --port 8765        # backend p/ o app macOS
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from . import cli_sink
from .config import load_config
from .events import EventSink, RunControl
from .loop import run as run_loop
from .status import SprintStatus


def _cmd_run(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    if not args.story and not args.epic:
        print("erro: informe --story <id> ou --epic <id>", file=sys.stderr)
        return 2
    sink = EventSink()
    sink.add_callback(cli_sink.render)
    control = RunControl(interactive_cli=True)

    async def _go():
        await run_loop(
            cfg, story=args.story, epic=args.epic, dry_run=args.dry_run,
            sink=sink, control=control,
        )

    try:
        asyncio.run(_go())
    except KeyboardInterrupt:
        print("\nabortado")
        return 130
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config)
    status = SprintStatus(cfg.sprint_status_file)
    print(f"sprint-status: {cfg.sprint_status_file}")
    for key, st in status.development_status().items():
        print(f"  {key}: {st}")
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # import tardio: fastapi/uvicorn só são necessários no modo serve
    from .server import serve

    serve(host=args.host, port=args.port)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="autopilot", description=__doc__)
    parser.add_argument("--config", default="autopilot.yaml", help="caminho do autopilot.yaml")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="roda o ciclo para uma story ou epic")
    g = p_run.add_mutually_exclusive_group(required=True)
    g.add_argument("--story", help="id da story (ex.: 7-2-create-api)")
    g.add_argument("--epic", help="id da epic (ex.: 7)")
    p_run.add_argument("--dry-run", action="store_true", help="não executa; só mostra o plano")
    p_run.set_defaults(func=_cmd_run)

    p_status = sub.add_parser("status", help="mostra o sprint-status.yaml")
    p_status.set_defaults(func=_cmd_status)

    p_serve = sub.add_parser("serve", help="backend HTTP+WebSocket para o app macOS")
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.set_defaults(func=_cmd_serve)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
