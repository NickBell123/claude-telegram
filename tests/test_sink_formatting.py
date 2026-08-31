"""TelegramSink formats only on the finalize edit, and never fails closed.

Intermediate streamed edits stay plain text on purpose. A stream truncates at
arbitrary points, so a mid-stream buffer routinely ends inside an unclosed
construct; sending that as MarkdownV2 invites a Telegram rejection, and a
rejected edit is invisible to the reader -- the message simply stops updating.
Formatting once, on the final edit, is the same policy the Hermes adapter uses
(REQUIRES_EDIT_FINALIZE).

The fallback is the safety net: if Telegram rejects the formatted text anyway,
resend it stripped rather than leaving the reader with a frozen message.
"""
import pytest

from claude_telegram.bot import TelegramSink


class FakeBot:
    def __init__(self, reject_markup: bool = False):
        self.reject_markup = reject_markup
        self.edits: list[dict] = []
        self.sends: list[dict] = []

    async def send_message(self, chat_id, text, parse_mode=None, **kw):
        self.sends.append({"text": text, "parse_mode": parse_mode})
        return type("M", (), {"message_id": 7})()

    async def edit_message_text(self, chat_id, message_id, text, parse_mode=None, **kw):
        self.edits.append({"text": text, "parse_mode": parse_mode})
        if parse_mode and self.reject_markup:
            raise ValueError("Can't parse entities: bad offset")


class FakeApp:
    def __init__(self, bot):
        self.bot = bot


def sink(**kw):
    bot = FakeBot(**kw)
    return TelegramSink(FakeApp(bot), "42"), bot


async def test_intermediate_edit_is_plain_text():
    s, bot = sink()
    await s.edit(1, "**bold** and a. dot")
    assert bot.edits[0]["parse_mode"] is None
    assert bot.edits[0]["text"] == "**bold** and a. dot"


async def test_final_edit_is_markdown_v2():
    s, bot = sink()
    await s.edit(1, "**bold** and a. dot", finalize=True)
    assert bot.edits[0]["parse_mode"] == "MarkdownV2"
    assert bot.edits[0]["text"] == "*bold* and a\\. dot"


async def test_final_edit_falls_back_to_stripped_plain_text_when_rejected():
    s, bot = sink(reject_markup=True)
    await s.edit(1, "**bold** and a. dot", finalize=True)
    assert len(bot.edits) == 2, "should retry once, unformatted"
    assert bot.edits[1]["parse_mode"] is None
    # The reader must not see stray backslashes from the failed attempt.
    assert bot.edits[1]["text"] == "bold and a. dot"


async def test_placeholder_send_stays_plain():
    s, bot = sink()
    await s.send("⚡ thinking…")
    assert bot.sends[0]["parse_mode"] is None


async def test_intermediate_edit_failure_is_still_swallowed():
    s, bot = sink(reject_markup=True)
    await s.edit(1, "plain")          # no parse_mode, so no raise
    assert len(bot.edits) == 1


async def test_oversized_formatted_text_skips_the_doomed_formatted_send():
    # Escaping inflates length (measured up to ~33% on punctuation-dense text),
    # so a buffer near the renderer's cap can exceed Telegram's 4096 limit once
    # formatted. Sending it anyway burns an API call on a guaranteed rejection.
    s, bot = sink()
    await s.edit(1, "a. " * 2000, finalize=True)
    assert len(bot.edits) == 1, "should not attempt the oversized formatted edit"
    assert bot.edits[0]["parse_mode"] is None
    assert len(bot.edits[0]["text"]) <= 4096


async def test_both_edits_failing_is_logged(caplog):
    # If the formatted edit AND the plain retry both fail (flood control), the
    # reader is left on a frozen message. That must not be silent.
    class AlwaysFails(FakeBot):
        async def edit_message_text(self, *a, **kw):
            raise RuntimeError("flood control exceeded")

    bot = AlwaysFails()
    s = TelegramSink(FakeApp(bot), "42")
    with caplog.at_level("WARNING"):
        await s.edit(1, "**hi**", finalize=True)
    assert any("final edit" in r.message.lower() for r in caplog.records), \
        caplog.records
