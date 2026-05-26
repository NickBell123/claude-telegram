#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$HOME/.claude-telegram"
ENV_FILE="$STATE_DIR/env"
UNIT_SRC="$ROOT/systemd/claude-telegram.service"
UNIT_DST="$HOME/.config/systemd/user/claude-telegram.service"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$STATE_DIR" "$(dirname "$UNIT_DST")" "$BIN_DIR"
chmod 700 "$STATE_DIR"

if [ ! -d "$ROOT/.venv" ]; then
    echo "creating venv…"
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
    echo "Setting up $ENV_FILE"
    read -r -p "Telegram bot token (from @BotFather): " TOKEN
    read -r -p "Your Telegram user ID (from @userinfobot): " USER_ID
    PUSH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    umask 077
    cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_ID=$USER_ID
DEFAULT_CHAT_ID=$USER_ID
PUSH_TOKEN=$PUSH_TOKEN
PUSH_URL=http://127.0.0.1:8787/push
EOF
    chmod 600 "$ENV_FILE"
    echo "wrote $ENV_FILE (chmod 600)"
else
    echo "$ENV_FILE already exists; leaving it alone"
fi

install -m 0755 "$ROOT/scripts/tg-push" "$BIN_DIR/tg-push"
echo "installed $BIN_DIR/tg-push"

install -m 0644 "$UNIT_SRC" "$UNIT_DST"
echo "installed $UNIT_DST"

loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram.service
systemctl --user status claude-telegram.service --no-pager
