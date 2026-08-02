# Typing Indicator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Show Telegram's native "typing…" chat action for the whole time a turn is generating, so a long turn never looks dead.

**Architecture:** A new `claude_telegram/presence.py` exposes an async context manager, `typing_action`, which sends `ChatAction.TYPING` immediately on enter and then re-sends it from a background task every 4 seconds until the block exits. `Bot._run_turn` becomes a thin wrapper that opens this context manager around the existing turn body, which moves unchanged into `Bot._generate`.

**Tech Stack:** Python 3.11+, `python-telegram-bot`, `pytest` with `pytest-asyncio` (`asyncio_mode = "auto"` in `pyproject.toml`).

## Global Constraints

- Spec: `docs/superpowers/specs/2026-08-02-typing-indicator-design.md`. Branch: `feat/typing-indicator`.
- Module is named `presence.py`, **never** `typing.py` — a module named `typing` inside the package shadows the stdlib module by name and misleads readers.
- `TYPING_INTERVAL_S = 4.0`. Telegram expires a chat action at ~5s; 4s leaves headroom.
- Nothing is added to `Config`. The `interval_s` parameter exists only so tests can drive the loop fast.
- **No exception from `send_chat_action` may ever escape into the turn.** A cosmetic indicator must not be able to kill a real response. One failed beat must not end the heartbeat.
- Scope is `_run_turn` only. `/push`, `/cwd`, `/cost`, `/reset`, `/cd`, `/stop`, and auth/rate-limit rejections send no chat action.
- Run tests with `.venv/bin/pytest`.

---

### Task 1: `presence.py` and its tests

**Files:**
- Create: `claude_telegram/presence.py`
- Test: `tests/test_presence.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `typing_action(bot, chat_id: str, interval_s: float = TYPING_INTERVAL_S)` — an
  `@asynccontextmanager`. `bot` is any object with
  `async send_chat_action(chat_id: str, action: str) -> None`. Also exports the module
  constant `TYPING_INTERVAL_S: float = 4.0`. Task 2 imports both names.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_presence.py`:

```python
import asyncio

import pytest

from claude_telegram.presence import TYPING_INTERVAL_S, typing_action


class FakeBot:
    """Records chat actions. `fail` makes every send raise."""

    def __init__(self, fail: bool = False):
        self.actions: list[tuple[str, str]] = []
        self.fail = fail

    async def send_chat_action(self, chat_id: str, action: str) -> None:
        self.actions.append((chat_id, action))
        if self.fail:
            raise RuntimeError("telegram is down")


@pytest.mark.asyncio
async def test_sends_action_immediately_on_enter():
    bot = FakeBot()
    async with typing_action(bot, "42", interval_s=10.0):
        # No sleeping: the first action must already be sent by __aenter__.
        assert bot.actions == [("42", "typing")]


@pytest.mark.asyncio
async def test_repeats_while_block_is_active():
    bot = FakeBot()
    async with typing_action(bot, "42", interval_s=0.01):
        await asyncio.sleep(0.06)
    assert len(bot.actions) >= 3
    assert all(a == ("42", "typing") for a in bot.actions)


@pytest.mark.asyncio
async def test_stops_after_exit():
    bot = FakeBot()
    async with typing_action(bot, "42", interval_s=0.01):
        await asyncio.sleep(0.03)
    count_at_exit = len(bot.actions)
    await asyncio.sleep(0.05)
    assert len(bot.actions) == count_at_exit


@pytest.mark.asyncio
async def test_send_failure_never_escapes_and_loop_continues():
    bot = FakeBot(fail=True)
    async with typing_action(bot, "42", interval_s=0.01):
        await asyncio.sleep(0.05)
    # Every send raised, yet the block completed and beats kept being attempted.
    assert len(bot.actions) >= 3


@pytest.mark.asyncio
async def test_body_exception_propagates_and_heartbeat_stops():
    bot = FakeBot()
    with pytest.raises(ValueError):
        async with typing_action(bot, "42", interval_s=0.01):
            await asyncio.sleep(0.02)
            raise ValueError("boom")
    count_at_exit = len(bot.actions)
    await asyncio.sleep(0.05)
    assert len(bot.actions) == count_at_exit


def test_interval_leaves_headroom_under_telegram_expiry():
    # Telegram expires a chat action at ~5s.
    assert TYPING_INTERVAL_S < 5.0
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_presence.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'claude_telegram.presence'`

- [ ] **Step 3: Write the implementation**

Create `claude_telegram/presence.py`:

```python
import asyncio
import contextlib
from contextlib import asynccontextmanager

from telegram.constants import ChatAction

# Telegram expires a chat action after about 5 seconds, so a turn that runs
# longer than that needs the action re-sent on a heartbeat.
TYPING_INTERVAL_S = 4.0


async def _send(bot, chat_id: str) -> None:
    """Send one typing action, swallowing anything Telegram throws.

    A cosmetic indicator must never be able to take down a real response, so
    network errors, 429s and revoked chats are ignored. Cancellation is not an
    error and is re-raised so the heartbeat can stop promptly.
    """
    try:
        await bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _beat(bot, chat_id: str, interval_s: float) -> None:
    """Re-send the action until cancelled. One failed beat does not end the loop."""
    while True:
        await asyncio.sleep(interval_s)
        await _send(bot, chat_id)


@asynccontextmanager
async def typing_action(bot, chat_id: str, interval_s: float = TYPING_INTERVAL_S):
    """Show Telegram's "typing…" action for as long as the block runs."""
    # Sent inline rather than in the task so the first action is guaranteed to
    # have happened by the time the block body starts.
    await _send(bot, chat_id)
    task = asyncio.create_task(_beat(bot, chat_id, interval_s))
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest tests/test_presence.py -v`
Expected: PASS, 6 tests.

- [ ] **Step 5: Commit**

```bash
git add claude_telegram/presence.py tests/test_presence.py
git commit -m "feat: add typing_action heartbeat context manager"
```

---

### Task 2: Wire it into `_run_turn`

**Files:**
- Modify: `claude_telegram/bot.py` (import block at lines 1-15; `_run_turn` at lines 184-232)
- Modify: `tests/test_bot_cwd_and_session.py` (`FakeBotApi`, lines 18-31)
- Modify: `tests/test_bot_concurrency.py` (`FakeBotApi`, lines 16-29)

**Interfaces:**
- Consumes: `typing_action` from `claude_telegram.presence` (Task 1).
- Produces: `Bot._generate(chat_id: str, text: str) -> None` holding the former `_run_turn`
  body; `Bot._run_turn` keeps its existing signature and is now a wrapper.

**Why both test files change:** `FakeBotApi` is duplicated in `tests/test_bot_cwd_and_session.py`
and `tests/test_bot_concurrency.py`, and neither has `send_chat_action`. Because Task 1
swallows every exception, an un-updated fake would make the indicator silently no-op in
tests — passing while proving nothing. Both fakes must record the calls.

- [ ] **Step 1: Add `send_chat_action` to both fakes**

In **both** `tests/test_bot_cwd_and_session.py` and `tests/test_bot_concurrency.py`, add
`self.actions` to `FakeBotApi.__init__` and the new method to the class:

```python
        self.actions: list[tuple[str, str]] = []
```

```python
    async def send_chat_action(self, chat_id: str, action: str) -> None:
        self.actions.append((chat_id, action))
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_bot_cwd_and_session.py`:

```python
@pytest.mark.asyncio
async def test_turn_shows_typing_action(tmp_path: Path):
    runner = ScriptedRunner([RunnerEvent("text", {"text": "hi"})])
    bot = _build_bot(tmp_path, [runner])
    await bot._run_turn("42", "hello")
    assert ("42", "typing") in bot.app.bot.actions


@pytest.mark.asyncio
async def test_cwd_command_sends_no_typing_action(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    await bot._handle_command("42", "cwd", "")
    assert bot.app.bot.actions == []
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `.venv/bin/pytest tests/test_bot_cwd_and_session.py -k typing -v`
Expected: `test_turn_shows_typing_action` FAILS on an empty `actions` list.
`test_cwd_command_sends_no_typing_action` passes already — it is a regression guard pinning
the scope boundary, and must keep passing after Step 4.

- [ ] **Step 4: Wire it in**

In `claude_telegram/bot.py`, add to the import block after the `log` import:

```python
from claude_telegram.presence import typing_action
```

Rename the existing `async def _run_turn` to `async def _generate`, leaving its body
completely unchanged, and add this new wrapper immediately above it:

```python
    async def _run_turn(self, chat_id: str, text: str) -> None:
        """Hold Telegram's typing action for the whole turn; the work lives in _generate."""
        async with typing_action(self.app.bot, chat_id):
            await self._generate(chat_id, text)
```

- [ ] **Step 5: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: PASS, all tests including the two new ones. The pre-existing `ChatAction` import
at `bot.py:7` is still unused by `bot.py` itself — leave it or drop it, but do not add a new
one; `presence.py` owns the constant now.

- [ ] **Step 6: Commit**

```bash
git add claude_telegram/bot.py tests/test_bot_cwd_and_session.py tests/test_bot_concurrency.py
git commit -m "feat: show typing indicator for the duration of a turn"
```

---

## Manual verification

After Task 2, run the bridge against the real bot and send a prompt that takes a while —
something with a slow `Bash` call. Confirm "typing…" appears in the chat header within a
second and persists past the 5-second mark, through gaps where the message is not being
edited, and clears when the ✅ lands.

Then trigger a cron push (`tg-push`) and confirm **no** typing indicator appears before it.
