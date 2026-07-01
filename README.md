# BMAD Autopilot Orchestrator

Autonomous orchestrator for the **[BMAD-METHOD](https://github.com/bmad-code-org/bmad-method) v6** implementation cycle.

It runs `bmad-create-story → bmad-dev-story → bmad-code-review` for each story (and
`bmad-retrospective` at the end of an epic) **without human intervention** — and every
decision a BMAD skill would normally stop and ask a human about is routed to a *second*
Claude session, the **advisor**, which inspects the codebase and the planning artifacts
and picks the option that best fits the project.

Each skill runs in a **fresh worker session** (clean context, as BMAD recommends); state
flows between phases through the BMAD artifacts (`sprint-status.yaml` + story files), not
through conversation memory.

> **Why not just let one session decide?** Tools like
> [robertguss/bmad_automated](https://github.com/robertguss/bmad_automated) run the loop
> by injecting *"don't ask questions, use your best judgment"* into the worker prompt. This
> project does the opposite: it **keeps** the questions and routes them to a dedicated,
> read-only advisor with an architect persona, so decisions are made deliberately and are
> auditable — every choice is logged with the question and the rationale.

---

## Table of contents

- [How it works](#how-it-works)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Usage (CLI)](#usage-cli)
- [macOS app (real-time UI)](#macos-app-real-time-ui)
- [Project layout](#project-layout)
- [Tests](#tests)
- [Before your first real run](#before-your-first-real-run)
- [Acknowledgements](#acknowledgements)

---

## How it works

```
┌──────────────────────────────────────────────────────────────────────┐
│  autopilot (Python)                                                    │
│  scope: a single story | a whole epic (+ retrospective)                │
│                                                                        │
│  per-story loop, driven by sprint-status.yaml:                         │
│    backlog        → bmad-create-story                                  │
│    ready-for-dev  → bmad-dev-story                                     │
│    in-progress    → bmad-dev-story                                     │
│    review         → bmad-code-review                                   │
│    done           → next story                                         │
│  all stories done → bmad-retrospective                                 │
│                                                                        │
│  each phase = a NEW worker session (ClaudeSDKClient running the skill) │
│      │ skill raises a decision (<ask> or AskUserQuestion)              │
│      ▼                                                                 │
│   ADVISOR (a 2nd, read-only Claude session with an architect persona)  │
│      │  reads code + epics/PRD/architecture, returns choice + reason   │
│      ▼  answer is injected back into the worker, which continues       │
│   phase complete ► deterministic git rules (branch/commit/PR/merge)    │
└──────────────────────────────────────────────────────────────────────┘
```

- **`status.py`** reads `sprint-status.yaml` (`development_status`) and, via **`router.py`**,
  decides the next phase from the story's status.
- **`worker.py`** runs the skill in a `ClaudeSDKClient` and intercepts decisions two ways:
  - **Structured:** the skill calls the `AskUserQuestion` tool → caught by `can_use_tool` → advisor.
  - **Safety net:** the skill emits an `<ask>` as plain text and pauses → we detect the question →
    advisor → we inject the answer as the next turn (`client.query()`).
- **`advisor.py`** is a separate read-only session (`Read`/`Grep`/`Glob` only) with an architect
  persona. It keeps a human-readable memory at `.autopilot/advisor-memory.md` and logs every
  decision to `.autopilot/logs/decisions.jsonl`.
- **`git_rules.py`** applies the per-phase git actions (branch / commit / PR / merge),
  deterministically, *between* skill invocations — so they don't depend on the worker.

---

## Requirements

- **Python 3.10+**
- **An authenticated Claude session** — the `claude-agent-sdk` uses your existing Claude login.
- **`git`** and **`gh`** (GitHub CLI) on `PATH` — required only if your git rules open/merge PRs.
- A BMAD **v6** project to point at.
- *(macOS app only)* macOS 14+ and a Swift toolchain (Xcode command-line tools).

---

## Installation

```bash
git clone git@github.com:FelipeSHenrique/bmad-autopilot-orchestrator.git
cd bmad-autopilot-orchestrator

python3 -m venv .venv
./.venv/bin/pip install -e .
```

The core (`autopilot run` / `autopilot status`) works with the line above. Two optional
extras are only needed for specific features:

```bash
# macOS app backend (the `autopilot serve` command)
./.venv/bin/pip install "fastapi" "uvicorn[standard]"

# running the test suite
./.venv/bin/pip install pytest httpx
```

---

## Configuration

Copy the example config and edit it for your project:

```bash
cp autopilot.example.yaml autopilot.yaml
```

```yaml
# autopilot.yaml
bmad_project_dir: /path/to/your/bmad-project

# Paths relative to the project. Confirm them in your install's
# _bmad/bmm/config.yaml (implementation_artifacts / planning_artifacts).
sprint_status_path: _bmad-output/implementation-artifacts/sprint-status.yaml
planning_artifacts_dir: _bmad-output/planning-artifacts

# How a skill is triggered via client.query(). {skill} and {story_id} are substituted.
# If your install exposes skills as slash-commands, change this accordingly, e.g.:
#   "/bmad:bmm:workflows:{skill_short} for {story_id}"
invoke_template: "Run the {skill} workflow for {story_id}."

models:
  worker: claude-opus-4-8
  advisor: claude-opus-4-8     # advisor >= worker

autonomy:
  human_checkpoint: none       # none | end-of-story | retrospective

max_turns_per_phase: 40
log_dir: .autopilot/logs

phases:
  bmad-create-story:
    git:
      - create_branch: "story/{story_id}"
      - commit: "story: draft {story_id}"
  bmad-dev-story:
    git:
      - commit: "feat: implement {story_id}"
  bmad-code-review:
    git:
      - commit: "review: {story_id}"
      - open_pr: { base: main, title: "{story_id}" }
      - merge_pr: { method: squash }
  bmad-retrospective:
    git:
      - commit: "chore: retrospective epic-{epic_id}"
```

> `autopilot.yaml` is git-ignored on purpose — it contains your local project path. Commit only
> `autopilot.example.yaml`.

---

## Usage (CLI)

```bash
autopilot status                       # inspect the project's sprint-status.yaml
autopilot run --story 7-2-create-api   # one story, from its current status until done
autopilot run --epic 7                 # the whole epic + retrospective
autopilot run --epic 7 --dry-run       # print the plan only — runs nothing, spends no tokens
```

- `--dry-run` walks the full lifecycle and prints the phases and git actions it *would*
  perform, without calling Claude or touching git. Always start here.
- Use `--config path/to/autopilot.yaml` to point at a config other than `./autopilot.yaml`.

Every advisor decision is appended to `.autopilot/logs/decisions.jsonl` (question, options,
choice, rationale, phase, timestamp) so you can review whether it decided correctly.

---

## macOS app (real-time UI)

A native SwiftUI app (plus a menu-bar item) to watch the autopilot **in real time**
(token-by-token streaming of both the worker and the advisor) and control it: pick a project
(it auto-detects existing / mid-development BMAD installs), choose scope, play/pause/stop,
toggle dry-run, and approve checkpoints. The console renders the conversation clearly —
worker messages, an "asked the advisor" banner, and the advisor's choice + rationale.

### Architecture

```
AutopilotApp (SwiftUI, swift build)  ──REST + WebSocket──►  autopilot serve (FastAPI)  ──►  core (loop/worker/advisor)
```

The backend is the `autopilot serve` subcommand (port 8765 by default). The app connects to an
already-running backend or spawns one itself.

### Run in development (quickest way to see it working)

```bash
# 1) backend
./.venv/bin/python -m autopilot serve --port 8765

# 2) app (in another terminal)
cd apps/macos
swift build && swift run            # or open it in Xcode (below)
```

Add a project with the sidebar's **＋** button (pick a BMAD project folder), see its
epics/stories with colored status badges, and press Play on a story or epic.

### How the app finds the backend

The app talks to the backend on `http://127.0.0.1:8765`. If it doesn't find one already
running, it spawns `./.venv/bin/python -m autopilot serve` from the repo. Point it at custom
paths with the `AUTOPILOT_REPO` / `AUTOPILOT_PYTHON` environment variables. The usual backend
requirements apply (Claude authenticated, `git`, `gh`).

It's a personal-use dev app — run it with `swift build` / `swift run`; there's no packaged
`.app` to install.

---

## Project layout

```
autopilot/            core (Python)
  config.py           load autopilot.yaml + per-project overrides
  status.py           read/write sprint-status.yaml; runnable/ordering rules
  router.py           status → phase mapping and lifecycle sequence
  detect.py           auto-detect an existing BMAD install
  advisor.py          read-only advisor session (persona, memory, decision log)
  worker.py           run a phase; intercept decisions; route to the advisor
  git_rules.py        per-phase git actions (branch/commit/PR/merge)
  loop.py             orchestrate scope (story|epic), phases, retrospective
  events.py           async event bus (EventSink) + run control (pause/stop)
  server.py           FastAPI backend (REST + WebSocket) for the macOS app
  cli_sink.py         terminal renderer for events
  __main__.py         CLI: run / status / serve

apps/macos/           native SwiftUI app + menu-bar item
tests/                pytest suite (core + backend integration, Claude faked)
```

---

## Tests

The suite mocks `ClaudeSDKClient`, so it makes **no real Claude calls and spends no tokens**.
It covers the core (status/router/git/ordering), the FastAPI backend (run, control/stop,
out-of-order rejection), and the worker→advisor integration flow.

```bash
./.venv/bin/pip install pytest httpx
./.venv/bin/python -m pytest -q
```

---

## Before your first real run

The deterministic core (status/router/git/CLI) is covered by tests via a fixture and dry-run.
The steps below depend on **your** BMAD v6 install, so verify them once:

1. **`invoke_template`** — how a skill is triggered via `client.query()`. The default uses a
   natural prompt (`"Run the {skill} workflow for {story_id}."`). If your install exposes skills
   as slash-commands (e.g. `/bmad:bmm:workflows:create-story`), update `invoke_template`.
2. **Real paths** of `implementation_artifacts` / `planning_artifacts` (see the project's
   `_bmad/bmm/config.yaml`).
3. **Decision mechanism** — run a single phase and check whether the worker turns `<ask>` into
   an `AskUserQuestion` (structured path) or falls back to the plain-text safety net. Adjust
   `WORKER_SYS` / `_looks_like_question` in `worker.py` if needed.

Recommended path: start with `--story <id> --dry-run`, then a real story on a throwaway branch,
reviewing `.autopilot/logs/decisions.jsonl` as you go.

---

## Acknowledgements

- [BMAD-METHOD](https://github.com/bmad-code-org/bmad-method) — the agile AI-driven development
  method this tool automates.
- [robertguss/bmad_automated](https://github.com/robertguss/bmad_automated) — prior art for
  automating the BMAD loop; this project takes the opposite stance on decisions (route to an
  advisor instead of suppressing the questions).
