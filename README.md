# claude-telegram

Telegram bridge to the `claude` CLI. Talk to Claude Code from your phone with full skill/MCP parity. Bonus: a localhost push endpoint so cron jobs can DM you.

## Install (Aspire host)

```bash
git clone <repo> ~/claude-telegram
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

## Files

- `~/.claude-telegram/env` — config (chmod 600)
- `~/.claude-telegram/state.json` — per-chat session IDs and cwd
- `~/.claude-telegram/log.jsonl` — append-only event log

## Logs

```bash
journalctl --user -u claude-telegram -f
tail -f ~/.claude-telegram/log.jsonl
```

## Safety

- Only the configured `ALLOWED_USER_ID` can talk to the bot. Other users are dropped silently.
- The push endpoint binds to `127.0.0.1` only and requires a bearer token.
- `claude` runs with `--dangerously-skip-permissions`. Same blast radius as you running Claude Code interactively. If your phone is lost, revoke the bot token via @BotFather.
