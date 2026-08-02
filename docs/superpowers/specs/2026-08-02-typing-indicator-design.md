# Typing indicator — Design Spec

**Date:** 2026-08-02
**Status:** Approved for implementation planning
**Scope:** `claude_telegram.bot._run_turn` only

## Goal

Show Telegram's native "typing…" chat action in the chat header for the whole time a turn
is being generated, so a long turn never looks dead.

## Why it is not a one-liner

Telegram expires a chat action after about 5 seconds. A single `send_chat_action` call
cannot cover a turn that runs for minutes, so the feature needs a heartbeat that re-sends
the action on an interval and is guaranteed to stop when the turn ends.

`ChatAction` is already imported in `bot.py` and never used — the feature was planned in
the original build and dropped. This spec finishes it and the dead import becomes live.

## Non-goals

- No indicator on the `/push` endpoint. Cron alerts are not generated responses, and
  showing "typing…" before an automated notification would misrepresent what happened.
- No indicator on instant command replies (`/cwd`, `/cost`, `/reset`, `/cd`, `/stop`) or on
  auth and rate-limit rejections.
- No user-facing on/off switch and nothing added to `Config`. Telegram's 5s expiry is fixed,
  so there is nothing worth tuning. The `interval_s` parameter exists so tests can drive the
  loop fast; production always uses the default.
- The existing `⚡ thinking…` placeholder message is unchanged. This adds the native chat
  affordance alongside it; it does not replace it.

## Architecture

```
_run_turn(chat_id, text)
  └── async with typing_action(bot, chat_id):     ← new
        renderer.start_placeholder()
        _stream_attempt(...)                      ← may run for minutes
        renderer.finalize(...)
      ← __aexit__ cancels the heartbeat on every path
```

## Component: `claude_telegram/presence.py`

One new module, roughly 40 lines, exposing a single async context manager:

```python
@asynccontextmanager
async def typing_action(bot, chat_id: str, interval_s: float = TYPING_INTERVAL_S): ...
```

**Named `presence.py`, not `typing.py`.** A module named `typing` inside the package
shadows the stdlib module by name. Python 3 absolute imports would tolerate it, but it
misleads every reader of the package, so the name is avoided deliberately.

**Behaviour**

- On enter: send `ChatAction.TYPING` once immediately, then start a background task.
- Loop: sleep `interval_s`, send the action again, repeat until cancelled.
- `TYPING_INTERVAL_S = 4.0`, a module constant. Telegram expires the action at 5s; 4s
  leaves headroom without spamming.
- On exit: cancel the task and await it, suppressing `CancelledError`.

**Error isolation — the load-bearing requirement**

Every `send_chat_action` call is wrapped so no exception can escape into the turn. A
network blip, a 429, or a revoked chat must never take down a real response for the sake of
a cosmetic indicator. Failures are swallowed and the loop continues; one failed beat does
not end the heartbeat.

## Concurrency

The existing per-chat `asyncio.Lock` in `Bot._lock_for` already serialises turns, so there
is exactly one heartbeat per chat at any time. `/stop` needs no special handling: it ends
`_run_turn`, which exits the context manager, which cancels the task.

## Testing

New `tests/test_presence.py`, following the existing fake-sink style. Tests use a fake bot
that records calls and pass a very small `interval_s`, so the suite stays fast without a
real 4-second wait:

1. The action is sent once on enter, before any sleep.
2. The action repeats while the block is active.
3. The task is cancelled on exit and no further actions are sent.
4. An exception raised by `send_chat_action` neither escapes the context manager nor stops
   subsequent beats.
5. The context manager still exits cleanly when the wrapped body raises.

One addition to the bot tests: a `/cwd` command sends no chat action, pinning the scope
boundary.
