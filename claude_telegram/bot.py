import asyncio
import os
from dataclasses import dataclass, field
from typing import Callable, Optional

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from claude_telegram.config import Config
from claude_telegram.log import JsonlLogger
from claude_telegram.presence import typing_action
from claude_telegram.ratelimit import SlidingWindowLimiter
from claude_telegram.runner import ClaudeRunner, RunnerEvent
from claude_telegram.state import StateStore
from claude_telegram.stream import MessageSink, StreamRenderer

KNOWN_COMMANDS = {"reset", "cd", "cwd", "cost", "stop"}


def is_authorized(user_id: Optional[int], allowed_user_id: int) -> bool:
    return user_id is not None and user_id == allowed_user_id


def parse_command(text: str) -> tuple[Optional[str], str]:
    """Return (command_name | None, remaining_text)."""
    if not text.startswith("/"):
        return (None, text)
    rest = text[1:]
    # split on first whitespace; preserve everything after
    parts = rest.split(None, 1)
    if not parts:
        return (None, "")
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd not in KNOWN_COMMANDS:
        return (None, text)
    return (cmd, arg)


@dataclass
class LastTurn:
    cost: float | None = None


# The CLI refuses --resume for a session it no longer has on disk. The stored
# session id then poisons every subsequent turn, so treat it as recoverable.
STALE_SESSION_MARKER = "No conversation found with session ID"


def is_stale_session_error(ev: RunnerEvent) -> bool:
    return ev.kind == "error" and STALE_SESSION_MARKER in str(ev.data.get("stderr", ""))


@dataclass
class _Attempt:
    """What one pass over the runner produced."""

    captured_session: str | None
    cost: float | None = None
    exception: Exception | None = None
    errors: list[RunnerEvent] = field(default_factory=list)
    produced_output: bool = False

    @property
    def stale_session(self) -> bool:
        return any(is_stale_session_error(e) for e in self.errors)


class TelegramSink(MessageSink):
    """MessageSink backed by python-telegram-bot."""

    def __init__(self, app: Application, chat_id: str):
        self.app = app
        self.chat_id = chat_id

    async def send(self, text: str) -> int:
        msg = await self.app.bot.send_message(chat_id=self.chat_id, text=text)
        return msg.message_id

    async def edit(self, message_id: int, text: str) -> None:
        try:
            await self.app.bot.edit_message_text(chat_id=self.chat_id, message_id=message_id, text=text)
        except Exception:
            # Edits can fail on identical text or rate limits; ignore.
            pass


class Bot:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        runner_factory: Callable[[], ClaudeRunner],
        logger: JsonlLogger,
        limiter: SlidingWindowLimiter,
    ):
        self.config = config
        self.state = state
        # A fresh runner per turn: each owns its own subprocess, so /stop in one
        # chat cannot terminate another chat's in-flight turn.
        self.runner_factory = runner_factory
        self.logger = logger
        self.limiter = limiter
        self._in_flight: dict[str, asyncio.Lock] = {}
        self._last_turn: dict[str, LastTurn] = {}
        self._current_runner: dict[str, ClaudeRunner] = {}
        self.app: Application = ApplicationBuilder().token(config.telegram_bot_token).build()
        self.app.add_handler(MessageHandler(filters.TEXT, self._on_message))

    async def send(self, chat_id: str, text: str) -> int:
        msg = await self.app.bot.send_message(chat_id=chat_id, text=text)
        return msg.message_id

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._in_flight.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._in_flight[chat_id] = lock
        return lock

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg or not msg.text:
            return
        if not is_authorized(user.id, self.config.allowed_user_id):
            self.logger.write("drop", chat_id=str(chat.id), details={"reason": "unauthorized", "user_id": user.id})
            return
        chat_id = str(chat.id)
        if not self.limiter.allow(chat_id):
            self.logger.write("drop", chat_id=chat_id, details={"reason": "rate_limited"})
            await self.app.bot.send_message(chat_id=chat_id, text="⛔ rate limit hit; try again later")
            return
        self.logger.write("inbound", chat_id=chat_id, details={"text": msg.text})

        cmd, arg = parse_command(msg.text)
        if cmd is not None:
            await self._handle_command(chat_id, cmd, arg)
            return

        async with self._lock_for(chat_id):
            await self._run_turn(chat_id, msg.text)

    async def _handle_command(self, chat_id: str, cmd: str, arg: str) -> None:
        if cmd == "reset":
            self.state.clear_session(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text="🔄 session cleared")
        elif cmd == "cd":
            if not arg:
                await self.app.bot.send_message(chat_id=chat_id, text="usage: /cd <path>")
                return
            if "\n" in arg or "\r" in arg:
                await self.app.bot.send_message(chat_id=chat_id, text="send one command per message")
                return
            # Resolve like a shell cd: relative to this chat's cwd, not to the
            # process working directory. Validate before persisting — an
            # unchecked path is written to state.json and then fails every
            # later turn at subprocess spawn.
            current = self.state.get(chat_id).cwd
            cwd = os.path.abspath(os.path.join(current, os.path.expanduser(arg)))
            if not os.path.isdir(cwd):
                await self.app.bot.send_message(chat_id=chat_id, text=f"⚠️ not a directory: {cwd}")
                return
            self.state.set_cwd(chat_id, cwd)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 cwd set to {cwd}")
        elif cmd == "cwd":
            s = self.state.get(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 {s.cwd}")
        elif cmd == "cost":
            last = self._last_turn.get(chat_id)
            if last and last.cost is not None:
                await self.app.bot.send_message(chat_id=chat_id, text=f"💰 last turn: ${last.cost:.4f}")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="no completed turn yet")
        elif cmd == "stop":
            r = self._current_runner.get(chat_id)
            if r:
                r.terminate()
                await self.app.bot.send_message(chat_id=chat_id, text="🛑 stopping")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="nothing running")

    async def _run_turn(self, chat_id: str, text: str) -> None:
        """Hold Telegram's typing action for the whole turn; the work lives in _generate."""
        async with typing_action(self.app.bot, chat_id):
            await self._generate(chat_id, text)

    async def _generate(self, chat_id: str, text: str) -> None:
        state = self.state.get(chat_id)
        sink = TelegramSink(self.app, chat_id)
        renderer = StreamRenderer(
            sink,
            max_chars=self.config.max_telegram_chars,
            min_edit_interval_s=self.config.edit_min_interval_s,
        )
        await renderer.start_placeholder()

        session_id = state.session_id
        attempt = await self._stream_attempt(chat_id, renderer, text, session_id, state.cwd)
        # One retry from a clean session when the CLI rejected a session id that
        # no longer exists — but only while the user has seen nothing, so the
        # retry cannot duplicate output that was already rendered.
        if attempt.stale_session and session_id is not None and not attempt.produced_output:
            self.logger.write("session_reset", chat_id=chat_id, details={"stale_session_id": session_id})
            self.state.clear_session(chat_id)
            session_id = None
            attempt = await self._stream_attempt(chat_id, renderer, text, None, state.cwd)

        error = attempt.exception
        failed = error is not None or bool(attempt.errors)
        if error is not None:
            try:
                await sink.send(f"⚠️ runner error: {type(error).__name__}: {error}")
            except Exception:
                pass
        else:
            for ev in attempt.errors:
                await renderer.handle(ev)
            try:
                await renderer.finalize(ok=not failed)
            except Exception:
                pass

        captured_session = attempt.captured_session
        if captured_session and captured_session != self.state.get(chat_id).session_id:
            self.state.set_session(chat_id, captured_session)
        self._last_turn[chat_id] = LastTurn(cost=attempt.cost)
        self.logger.write(
            "claude",
            chat_id=chat_id,
            details={
                "cost_usd": attempt.cost,
                "session_id": captured_session,
                "error": f"{type(error).__name__}: {error}" if error else None,
            },
        )

    async def _stream_attempt(
        self,
        chat_id: str,
        renderer: StreamRenderer,
        text: str,
        session_id: Optional[str],
        cwd: str,
    ) -> _Attempt:
        """Run the prompt once, rendering as it goes. Error events are held back
        so a recoverable failure can be retried without the user ever seeing it."""
        runner = self.runner_factory()
        self._current_runner[chat_id] = runner
        out = _Attempt(captured_session=session_id)
        try:
            async for ev in runner.run(prompt=text, session_id=session_id, cwd=cwd):
                if ev.kind == "session" and ev.data.get("session_id"):
                    out.captured_session = ev.data["session_id"]
                if ev.kind == "result":
                    out.cost = ev.data.get("cost_usd")
                    if ev.data.get("session_id"):
                        out.captured_session = ev.data["session_id"]
                if ev.kind == "error":
                    out.errors.append(ev)
                    continue
                if ev.kind in ("text", "tool_use"):
                    out.produced_output = True
                await renderer.handle(ev)
        except Exception as e:
            out.exception = e
        finally:
            self._current_runner.pop(chat_id, None)
        return out
