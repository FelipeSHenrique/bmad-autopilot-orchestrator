#!/usr/bin/env bash
# Empacota o backend Python (autopilot serve) num binário único via PyInstaller
# e o coloca em apps/macos/Resources/autopilot-backend, de onde o BackendController
# (release) o lança — produzindo um .app autocontido.
#
# Uso:  ./apps/macos/build_backend.sh
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VENV_PY="$REPO_ROOT/.venv/bin/python"
RES_DIR="$REPO_ROOT/apps/macos/Resources"

cd "$REPO_ROOT"
"$VENV_PY" -m pip install --quiet pyinstaller

# Entry-point: chama autopilot serve. PyInstaller precisa de um script alvo.
ENTRY="$(mktemp -t autopilot_entry).py"
cat > "$ENTRY" <<'PY'
import sys
from autopilot.__main__ import main
sys.exit(main(["serve", *sys.argv[1:]]))
PY

mkdir -p "$RES_DIR"
"$VENV_PY" -m PyInstaller \
  --onefile \
  --name autopilot-backend \
  --collect-all claude_agent_sdk \
  --collect-all fastapi \
  --collect-all uvicorn \
  --collect-submodules autopilot \
  --paths "$REPO_ROOT" \
  --distpath "$RES_DIR" \
  --workpath "$REPO_ROOT/.build-pyinstaller" \
  --specpath "$REPO_ROOT/.build-pyinstaller" \
  "$ENTRY"

rm -f "$ENTRY"
echo "✔ backend embutido em $RES_DIR/autopilot-backend"
echo "Agora rode: cd apps/macos && xcodegen generate && xcodebuild ... (o binário entra no .app)"
