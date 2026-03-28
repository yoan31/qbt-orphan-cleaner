#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# Créer le venv s'il n'existe pas encore
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[*] Création du venv..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/qbt_orphan_cleaner.py" "$@"
