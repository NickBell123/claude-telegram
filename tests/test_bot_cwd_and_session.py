import os
from pathlib import Path

import pytest

from claude_telegram.bot import Bot
from claude_telegram.config import Config
from claude_telegram.log import JsonlLogger
from claude_telegram.ratelimit import SlidingWindowLimiter
from claude_telegram.runner import RunnerEvent
from claude_telegram.state import StateStore

FAKE_TOKEN = "123456:AAHfakefakefakefakefakefakefakefake"

STALE_SESSION_STDERR = "No conversation found with session ID: bf0d22b8-4c2b-43cc-a002-4fed130425fe\n"


class FakeBotApi:
    def __init__(self):
        self.sent: list[tuple[str, str]] = []
        self.edits: list[tuple[int, str]] = []
        self._id = 0

    async def send_message(self, chat_id: str, text: str):
        self._id += 1
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": self._id})()

    async def edit_message_text(self, chat_id: str, message_id: int, text: str) -> None:
        self.edits.append((message_id, text))


class FakeApp:
    def __init__(self):
        self.bot = FakeBotApi()


class ScriptedRunner:
    """Yields a fixed event list and records the session_id/cwd it was handed."""

    def __init__(self, events: list[RunnerEvent]):
        self.events = events
        self.seen_session: str | None = None
        self.seen_cwd: str | None = None
        self.called = False

    async def run(self, prompt: str, session_id, cwd: str):
        self.called = True
        self.seen_session = session_id
        self.seen_cwd = cwd
        for ev in self.events:
            yield ev

    def terminate(self) -> None:
        pass


def _build_bot(tmp_path: Path, runners: list) -> Bot:
    cfg = Config(
        telegram_bot_token=FAKE_TOKEN,
        allowed_user_id=42,
        default_chat_id="42",
        push_token="secret",
        state_dir=str(tmp_path),
    )
    handed_out = iter(runners)
    bot = Bot(
        config=cfg,
        state=StateStore(tmp_path / "state.json", default_cwd=str(tmp_path)),
        runner_factory=lambda: next(handed_out),
        logger=JsonlLogger(tmp_path / "log.jsonl"),
        limiter=SlidingWindowLimiter(max_events=100, window_seconds=3600),
    )
    bot.app = FakeApp()
    return bot


def _transcript(bot: Bot) -> str:
    """Everything the user actually saw, sent messages and edits alike."""
    return "\n".join([t for _, t in bot.app.bot.sent] + [t for _, t in bot.app.bot.edits])


# --- /cd validation -------------------------------------------------------


@pytest.mark.asyncio
async def test_cd_to_missing_directory_is_rejected_and_not_persisted(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    original = bot.state.get("chat-a").cwd

    await bot._handle_command("chat-a", "cd", str(tmp_path / "nope"))

    assert bot.state.get("chat-a").cwd == original
    assert "not a directory" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_cd_to_a_file_is_rejected(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    f = tmp_path / "a-file"
    f.write_text("x")
    original = bot.state.get("chat-a").cwd

    await bot._handle_command("chat-a", "cd", str(f))

    assert bot.state.get("chat-a").cwd == original
    assert "not a directory" in bot.app.bot.sent[-1][1]


@pytest.mark.asyncio
async def test_cd_resolves_relative_path_against_current_cwd(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    start = tmp_path / "project"
    (start / "docs").mkdir(parents=True)
    bot.state.set_cwd("chat-a", str(start))

    await bot._handle_command("chat-a", "cd", "docs")

    assert bot.state.get("chat-a").cwd == str(start / "docs")


@pytest.mark.asyncio
async def test_cd_stores_an_absolute_normalised_path(tmp_path: Path):
    bot = _build_bot(tmp_path, [])
    (tmp_path / "project").mkdir()
    bot.state.set_cwd("chat-a", str(tmp_path / "project"))

    await bot._handle_command("chat-a", "cd", "..")

    stored = bot.state.get("chat-a").cwd
    assert os.path.isabs(stored)
    assert stored == str(tmp_path)


# --- stale session recovery ----------------------------------------------


@pytest.mark.asyncio
async def test_stale_session_is_cleared_and_the_turn_retried_fresh(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    fresh = ScriptedRunner([
        RunnerEvent(kind="session", data={"session_id": "new-session"}),
        RunnerEvent(kind="text", data={"text": "hello from the retry"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.02, "session_id": "new-session"}),
    ])
    bot = _build_bot(tmp_path, [dead, fresh])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert fresh.called, "a stale session should trigger one retry"
    assert fresh.seen_session is None, "the retry must run without --resume"
    assert bot.state.get("chat-a").session_id == "new-session"


@pytest.mark.asyncio
async def test_stale_session_retry_hides_the_error_and_shows_the_reply(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    fresh = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "hello from the retry"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.02, "session_id": "new-session"}),
    ])
    bot = _build_bot(tmp_path, [dead, fresh])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "No conversation found" not in seen
    assert "hello from the retry" in seen


@pytest.mark.asyncio
async def test_stale_session_is_not_retried_twice(tmp_path: Path):
    dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    also_dead = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    third = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [dead, also_dead, third])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert third.called is False, "only one retry is allowed"
    assert "No conversation found" in _transcript(bot), "a second failure must surface"


@pytest.mark.asyncio
async def test_partial_output_before_a_stale_error_is_not_retried(tmp_path: Path):
    """If the user already saw a reply, silently re-running would duplicate work."""
    chatty = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "some real output"}),
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": STALE_SESSION_STDERR}),
    ])
    second = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [chatty, second])
    bot.state.set_session("chat-a", "bf0d22b8-4c2b-43cc-a002-4fed130425fe")

    await bot._run_turn("chat-a", "hi")

    assert second.called is False


@pytest.mark.asyncio
async def test_unrelated_runner_error_is_not_retried(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "some other failure\n"}),
    ])
    second = ScriptedRunner([RunnerEvent(kind="text", data={"text": "should not run"})])
    bot = _build_bot(tmp_path, [boom, second])
    bot.state.set_session("chat-a", "sess-1")

    await bot._run_turn("chat-a", "hi")

    assert second.called is False
    assert "some other failure" in _transcript(bot)


# --- failed turns must not look successful --------------------------------


@pytest.mark.asyncio
async def test_failed_turn_is_not_marked_with_a_success_tick(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "some other failure\n"}),
    ])
    bot = _build_bot(tmp_path, [boom])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "✅" not in seen, "an errored turn must not render a success tick"
    assert "❌" in seen


@pytest.mark.asyncio
async def test_successful_turn_still_gets_a_tick_and_cost(tmp_path: Path):
    ok = ScriptedRunner([
        RunnerEvent(kind="text", data={"text": "all good"}),
        RunnerEvent(kind="result", data={"cost_usd": 0.0123, "session_id": "s1"}),
    ])
    bot = _build_bot(tmp_path, [ok])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "✅" in seen
    assert "$0.0123" in seen


@pytest.mark.asyncio
async def test_runner_error_renders_stderr_not_a_raw_dict(tmp_path: Path):
    boom = ScriptedRunner([
        RunnerEvent(kind="error", data={"returncode": 1, "stderr": "boom: it broke\n"}),
    ])
    bot = _build_bot(tmp_path, [boom])

    await bot._run_turn("chat-a", "hi")

    seen = _transcript(bot)
    assert "boom: it broke" in seen
    assert "'returncode'" not in seen, "should not dump the raw event dict"
