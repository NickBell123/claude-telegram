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
