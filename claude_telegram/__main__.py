import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from claude_telegram.bot import Bot
from claude_telegram.config import Config
from claude_telegram.log import JsonlLogger
from claude_telegram.push import build_app
from claude_telegram.ratelimit import SlidingWindowLimiter
from claude_telegram.runner import ClaudeRunner
from claude_telegram.state import StateStore


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    # httpx logs every request URL at INFO, and the Telegram API carries the bot
    # token in the path — that would write the token to the journal in plaintext.
    logging.getLogger("httpx").setLevel(logging.WARNING)


async def _main() -> None:
    configure_logging()
    cfg = Config.from_env()
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state = StateStore(state_dir / "state.json")
    logger = JsonlLogger(state_dir / "log.jsonl")
    limiter = SlidingWindowLimiter(max_events=cfg.rate_limit_per_hour, window_seconds=3600)
    bot = Bot(
        config=cfg,
        state=state,
        runner_factory=lambda: ClaudeRunner(claude_cmd=[cfg.claude_bin]),
        logger=logger,
        limiter=limiter,
    )

    async def push_send(chat_id: str, text: str) -> int:
        return await bot.send(chat_id, text)

    push_app = build_app(
        push_token=cfg.push_token,
        default_chat_id=cfg.default_chat_id,
        send=push_send,
    )

    # Start Telegram polling and push server concurrently.
    await bot.app.initialize()
    await bot.app.start()
    polling = asyncio.create_task(bot.app.updater.start_polling())

    runner_app = web.AppRunner(push_app)
    await runner_app.setup()
    site = web.TCPSite(runner_app, cfg.push_host, cfg.push_port)
    await site.start()
    logging.info("listening on %s:%d", cfg.push_host, cfg.push_port)

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        polling.cancel()
        await bot.app.updater.stop()
        await bot.app.stop()
        await bot.app.shutdown()
        await runner_app.cleanup()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
