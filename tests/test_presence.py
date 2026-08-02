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
