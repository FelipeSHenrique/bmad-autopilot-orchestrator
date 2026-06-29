"""Fixtures compartilhadas dos testes."""

import shutil
import subprocess
from pathlib import Path

import pytest

FIXTURE = Path(__file__).parent / "fixture_project"
SPRINT_REL = "_bmad-output/implementation-artifacts/sprint-status.yaml"


@pytest.fixture
def fixture_project() -> Path:
    return FIXTURE


@pytest.fixture
def sprint_status_file() -> Path:
    return FIXTURE / SPRINT_REL


@pytest.fixture
def tmp_project(tmp_path: Path) -> Path:
    """Cópia gravável do fixture_project (para testes que mutam status/config)."""
    dst = tmp_path / "proj"
    shutil.copytree(FIXTURE, dst)
    return dst


@pytest.fixture
def git_project(tmp_project: Path) -> Path:
    """Cópia do fixture já inicializada como repo git (p/ as regras de git locais)."""
    def run(*args):
        subprocess.run(["git", *args], cwd=tmp_project, check=True,
                       capture_output=True, text=True)
    run("init", "-q")
    run("config", "user.email", "test@example.com")
    run("config", "user.name", "Test")
    run("add", "-A")
    run("commit", "-q", "-m", "init")
    return tmp_project


@pytest.fixture
def fake_claude(monkeypatch):
    """Instala o FakeClaudeSDKClient em worker/advisor. Devolve o Recorder."""
    import fakes
    return fakes.install(monkeypatch)
