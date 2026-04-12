#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

# ── Read WEB_PORT from .env (fallback: 9090) ──────────────────────────────────
WEB_PORT=9090
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    _p=$(grep -E '^WEB_PORT=' "$SCRIPT_DIR/.env" | cut -d= -f2 | tr -d '[:space:]"' | head -1)
    [[ -n "$_p" ]] && WEB_PORT="$_p"
fi

# ── Extract version from source ───────────────────────────────────────────────
VERSION=$(grep -m1 '__version__' "$SCRIPT_DIR/qbt_orphan_cleaner.py" \
    | grep -oP '\d+\.\d+\.\d+' 2>/dev/null || echo "")

# ── Banner ────────────────────────────────────────────────────────────────────
cat << 'BANNER'

  ___  ___  _     ___           _
 / _ \| _ )| |_  / _ \ _ _ _ __| |_  __ _ _ _
| (_) | _ \|  _|| (_) | '_| '_ \ ' \/ _` | ' \
 \__\_|___/ \__| \___/|_| | .__/_||_\__,_|_||_|
   ___  _                  |_|
  / __|| | ___ __ _ _ _  ___ _ _
 | (__ | |/ -_) _` | ' \/ -_) '_|
  \___|_\___\__,_|_||_\___|_|

BANNER

[[ -n "$VERSION" ]] && printf '  v%s\n\n' "$VERSION"

# ── Clickable URL (OSC 8 — degrades gracefully on unsupported terminals) ──────
_url="http://localhost:${WEB_PORT}"
printf '  Ready on  \033]8;;%s\033\\%s\033]8;;\033\\\n\n' "$_url" "$_url"

# ── Venv setup ────────────────────────────────────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    echo "[*] Creating venv..."
    python3 -m venv "$VENV_DIR"
    "$VENV_DIR/bin/pip" install --quiet --upgrade pip
    "$VENV_DIR/bin/pip" install --quiet -r "$SCRIPT_DIR/requirements.txt"
fi

# ── Open browser after a short delay (background) ────────────────────────────
_url="http://localhost:${WEB_PORT}"
(sleep 1 && xdg-open "$_url" 2>/dev/null || open "$_url" 2>/dev/null || true) &

exec "$VENV_DIR/bin/python" "$SCRIPT_DIR/web.py" "$@"
