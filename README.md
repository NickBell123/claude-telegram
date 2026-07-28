# claude-telegram

[![tests](https://github.com/NickBell123/claude-telegram/actions/workflows/tests.yml/badge.svg)](https://github.com/NickBell123/claude-telegram/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Telegram bridge to the `claude` CLI. Talk to Claude Code from your phone with full skill/MCP parity. Bonus: a localhost push endpoint so cron jobs can DM you.

Each message spawns `claude -p --resume <session> --output-format stream-json` as a subprocess and streams the output back into a live-edited Telegram message. Wrapping the real CLI — rather than rebuilding on the Agent SDK — means every skill, MCP server, and hook you already have configured just works, with no duplicated config.

## Requirements

- Linux with a systemd user session (`systemctl --user`)
- Python 3.12+
- The [`claude` CLI](https://claude.com/claude-code), installed and authenticated

## Install

```bash
git clone https://github.com/NickBell123/claude-telegram ~/claude-telegram
cd ~/claude-telegram
./scripts/install.sh
```

You'll be prompted for:
- **Telegram bot token** — create one via [@BotFather](https://t.me/BotFather).
- **Your Telegram user ID** — DM [@userinfobot](https://t.me/userinfobot) to get it.

The installer writes `~/.claude-telegram/env`, installs `tg-push` to `~/.local/bin/`, enables a user systemd service, and starts it.

## Commands

- `/reset` — clear the conversation, start fresh
- `/cd <path>` — change Claude's working directory for this chat
- `/cwd` — show current working directory
- `/cost` — show cost of the last turn
- `/stop` — interrupt the in-flight turn

## Pushing from cron

```bash
echo "BTC +2.1%" | tg-push
tg-push --title "Morning brief" --file /tmp/brief.md
```

`examples/morning-brief.sh` is a worked example: a cron job that runs `claude -p`
headless and delivers the result to Telegram, including the PATH handling cron
needs and a failure path so a broken job doesn't fail silently.

## Files

- `~/.claude-telegram/env` — config (chmod 600)
- `~/.claude-telegram/state.json` — per-chat session IDs and cwd
- `~/.claude-telegram/log.jsonl` — append-only event log

## Logs

```bash
journalctl --user -u claude-telegram -f
tail -f ~/.claude-telegram/log.jsonl
```

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/pytest
```

## Safety

- Only the configured `ALLOWED_USER_ID` can talk to the bot. Other users are dropped silently.
- The push endpoint binds to `127.0.0.1` only and requires a bearer token.
- `claude` runs with `--dangerously-skip-permissions`. Same blast radius as you running Claude Code interactively. If your phone is lost, revoke the bot token via @BotFather.
- Built for a single operator. One `claude` subprocess runs at a time per chat; `/stop` targets the in-flight turn.

## License

MIT — see [LICENSE](LICENSE).
