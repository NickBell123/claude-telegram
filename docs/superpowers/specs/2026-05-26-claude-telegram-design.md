# claude-telegram — Design Spec

**Date:** 2026-05-26
**Status:** Approved for implementation planning
**Host:** Aspire (`/home/nick`)

## Goal

A Telegram bot that bridges to Claude Code with full parity — every skill, MCP server, hook, and setting available on the Aspire host is available from Nick's phone. Plus a localhost push endpoint so existing crons/scripts can send proactive messages to Telegram.

## Non-goals

- Multi-user support beyond a single Telegram user ID
- Webhook deployment (Aspire is behind NAT; long-polling only)
- Browser/voice/file-attachment handling in v1 (text only)
- Persisted memory beyond Claude's own session resume mechanism

## Architecture

```
Telegram ──long-poll──▶ bot.py ──spawn──▶ claude -p --resume <session>
                          │                       │     --output-format stream-json
                          │◀──── stdout (stream-json events) ─────────┘
                          │
                          ├──▶ Telegram editMessage (live streaming reply)
                          │
                          └──▶ aiohttp 127.0.0.1:8787 ──POST /push──▶ Telegram sendMessage
                                       ▲
                                       └── cron jobs / scripts push alerts
```

Single Python process containing:

- **TelegramBot** — `python-telegram-bot`, long-polling, no public webhook.
- **PushServer** — `aiohttp` on `127.0.0.1:8787`, bearer-token auth.
- **ClaudeRunner** — shared component owning sessions and spawning the `claude` CLI.

Project root: `/home/nick/claude-telegram/`.
Deployed as user-systemd unit `claude-telegram.service` with `loginctl enable-linger nick`.

## Components

### TelegramBot
- Long-polls Telegram for updates.
- **Auth gate:** drops any update where `from.id != ALLOWED_USER_ID`. Silent drop, no reply.
- Routes commands (`/reset`, `/cd`, `/cwd`, `/cost`, `/stop`) vs. free-form messages.
- Maintains an in-flight-turn flag per chat; queues additional messages until the current turn finishes.

### ClaudeRunner
- Spawns `claude -p "<msg>" [--resume <session_id>] --output-format stream-json --dangerously-skip-permissions` with `cwd` set per-chat.
- Captures `session_id` from the first stream event on a new session and persists it.
- Streams stdout line-by-line, parses stream-json events, emits structured updates to the TelegramBot.
- Supports SIGTERM via `/stop`.

### PushServer
- Single endpoint `POST /push` on `127.0.0.1:8787`.
- Bearer-token auth (`Authorization: Bearer $PUSH_TOKEN`).
- Body: `{"text": str, "chat_id"?: str, "parse_mode"?: str}`.
- `chat_id` defaults to `DEFAULT_CHAT_ID` env var.
- Auto-chunks text > 4000 chars.
- Returns `{"ok": true, "message_ids": [...]}`.

### tg-push CLI helper
- Installed to `~/.local/bin/tg-push`.
- Reads `PUSH_TOKEN` from `~/.claude-telegram/env`.
- Usage:
  ```bash
  echo "deploy finished" | tg-push
  tg-push --title "Pionex daily" --file /tmp/brief.md
  ```

## Session lifecycle

State at `~/.claude-telegram/state.json`:
```json
{
  "chats": {
    "<chat_id>": { "session_id": "abc123", "cwd": "/home/nick" }
  }
}
```

**Per-message flow:**
1. Update arrives → auth gate.
2. If no session for chat → spawn fresh; capture `session_id` from first event; persist.
3. If session exists → spawn with `--resume <session_id>`.
4. Send placeholder Telegram message (`⚡ thinking…`).
5. As stream-json events arrive, accumulate assistant text; edit the placeholder at most every 1.2s.
6. When accumulated text > 3900 chars, finalize current message and start a new one.
7. Tool-use events render inline: `> 🔧 Bash: ls /home/nick`.
8. Final message marked with `✅` plus token/cost summary from the final event.

**Commands:**
- `/reset` — drop session for this chat; next message starts fresh.
- `/cd <path>` — change Claude's cwd for this chat; persisted.
- `/cwd` — show current cwd.
- `/cost` — show cost of last turn.
- `/stop` — SIGTERM the in-flight claude subprocess.

**Concurrency:** one in-flight turn per chat; queue additional messages. Multiple chats run in parallel.

## Configuration

`~/.claude-telegram/env` (chmod 600):
```
TELEGRAM_BOT_TOKEN=...           # from @BotFather
ALLOWED_USER_ID=123456789        # Nick's numeric Telegram ID (via @userinfobot)
DEFAULT_CHAT_ID=123456789        # usually same as above
PUSH_TOKEN=<random 32 bytes hex> # generated at install
```

## Safety

Layered defenses, in order of precedence:

1. **Telegram auth gate** — drop updates from any user ID other than `ALLOWED_USER_ID`. Silent drop avoids leaking the bot's existence.
2. **Push endpoint** — bound to `127.0.0.1` only + bearer token. Anything reaching it already has localhost access to Aspire.
3. **Subprocess isolation** — Claude runs as Nick's user with `--dangerously-skip-permissions`. Same blast radius as Nick running Claude Code interactively. Accepted tradeoff: this is the "GOD MODE" the user asked for; documented here.
4. **Rate limit** — max 60 inbound messages/hour per chat as a kill switch against runaway loops or a stolen phone. Excess logged and dropped.
5. **`/stop` command** — interrupts the current turn via SIGTERM.

## Logging

Append-only `~/.claude-telegram/log.jsonl`. Every entry:
```json
{"ts": "...", "kind": "inbound|push|claude|drop", "chat_id": "...", "details": {...}}
```
Each `claude` entry records duration and cost from the final stream-json event.

## Deployment

- Python venv at `/home/nick/claude-telegram/.venv`.
- User systemd unit at `~/.config/systemd/user/claude-telegram.service`.
- `loginctl enable-linger nick` so the service survives logout.
- `install.sh` script:
  1. Creates venv, installs requirements.
  2. Prompts for `TELEGRAM_BOT_TOKEN` and `ALLOWED_USER_ID`.
  3. Generates `PUSH_TOKEN` (`secrets.token_hex(32)`).
  4. Writes `~/.claude-telegram/env` (chmod 600) and `state.json`.
  5. Writes systemd unit, runs `systemctl --user daemon-reload && systemctl --user enable --now claude-telegram`.
  6. Installs `tg-push` into `~/.local/bin/`.

## Testing

- **Unit tests:** auth gate, session persistence, stream chunking and 4000-char splitting, push bearer-token auth, rate limiter.
- **Integration test:** mock Telegram client + mock `claude` subprocess emitting canned stream-json events; assert end-to-end flow (placeholder → edits → final message with cost).
- No live Telegram calls in CI.

## Dependencies

- `python-telegram-bot >= 21`
- `aiohttp`
- `pytest`, `pytest-asyncio` (dev)
- System: `claude` CLI on `$PATH`, `systemd --user`.

## Open questions

None — all design decisions resolved during brainstorming.
