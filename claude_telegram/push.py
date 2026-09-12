import hmac
import logging
from typing import Awaitable, Callable, Optional

from aiohttp import web

from claude_telegram.richtext import (
    strip_markers,
    to_markdown_v2,
    unwrap_outer_markdown_fence,
)

SendFn = Callable[[str, str, Optional[str]], Awaitable[int]]

# Telegram rejects anything else with an opaque 400. Validate here so a cron job
# gets a useful error instead of a 500 from a failed send.
PARSE_MODES = {"MarkdownV2", "HTML", "Markdown"}

logger = logging.getLogger(__name__)


def _chunks(text: str, n: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + n] for i in range(0, len(text), n)]


def _chunks_by_line(text: str, n: int) -> list[str]:
    """Pack whole lines into chunks, falling back to a hard split for a long line.

    Splitting blindly on character count cuts through markup — half a bold run
    in each chunk — and Telegram rejects both halves. Alert formatting rarely
    spans a line, so line boundaries are a cheap way to keep runs intact
    without parsing entities. A single line over the limit has no boundary to
    use, and truncating would be worse than a split.
    """
    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        if len(line) > n:
            if cur:
                out.append(cur)
                cur = ""
            out.extend(_chunks(line, n))
            continue
        candidate = f"{cur}\n{line}" if cur else line
        if len(candidate) > n:
            out.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        out.append(cur)
    return out or [""]


def _chunks_markdown(text: str, n: int) -> list[str]:
    """Pack whole lines into chunks whose CONVERTED form fits in n characters.

    Two things force this to be separate from _chunks_by_line. Escaping
    inflates text — a line of dots doubles in length — so measuring the raw
    text lets a chunk cross Telegram's limit once converted. And chunking the
    already-converted text risks a split between a backslash and the character
    it escapes, which Telegram rejects. So chunk the raw markdown, but size
    each chunk by what it converts to.

    Returns raw chunks; the caller converts each one.
    """
    def fits(chunk: str) -> bool:
        return len(to_markdown_v2(chunk)) <= n

    def split_long_line(line: str) -> list[str]:
        # No line boundary to use. Grow greedily by raw characters, measuring
        # the converted length, so the pieces are as large as they can be.
        out: list[str] = []
        rest = line
        while rest:
            lo, hi = 1, len(rest)
            if fits(rest):
                out.append(rest)
                break
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if fits(rest[:mid]):
                    lo = mid
                else:
                    hi = mid - 1
            out.append(rest[:lo])
            rest = rest[lo:]
        return out

    out: list[str] = []
    cur = ""
    for line in text.split("\n"):
        candidate = f"{cur}\n{line}" if cur else line
        if fits(candidate):
            cur = candidate
            continue
        if cur:
            out.append(cur)
            cur = ""
        if fits(line):
            cur = line
        else:
            out.extend(split_long_line(line))
    if cur:
        out.append(cur)
    return out or [""]


def build_app(
    push_token: str,
    default_chat_id: str,
    send: SendFn,
    max_chars: int = 4000,
) -> web.Application:
    async def handle_push(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        # Constant-time compare so a caller can't time-probe the token byte by byte.
        if not hmac.compare_digest(auth, f"Bearer {push_token}"):
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        text = body.get("text")
        if not isinstance(text, str) or not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        chat_id = body.get("chat_id") or default_chat_id
        parse_mode = body.get("parse_mode")
        if parse_mode is not None and parse_mode not in PARSE_MODES:
            return web.json_response(
                {"ok": False, "error": f"parse_mode must be one of {sorted(PARSE_MODES)}"},
                status=400,
            )
        # "markdown": the caller is sending ordinary GitHub-flavoured markdown
        # (an LLM-written brief) and wants it rendered. We own the conversion,
        # using the same converter as the chat path, so cron scripts never have
        # to know MarkdownV2's 18 reserved characters.
        markdown = bool(body.get("markdown"))
        if markdown and parse_mode is not None:
            return web.json_response(
                {"ok": False, "error": "markdown and parse_mode are mutually exclusive"},
                status=400,
            )
        # The title is bolded server-side rather than by the caller, so the
        # fence unwrap above sees the model's output as the whole message.
        title = body.get("title")
        if title is not None and not isinstance(title, str):
            return web.json_response(
                {"ok": False, "error": "title must be a string"}, status=400)
        if title and not markdown:
            return web.json_response(
                {"ok": False, "error": "title requires markdown"}, status=400)
        ids = []
        if markdown:
            text = unwrap_outer_markdown_fence(text)
            if title:
                text = f"**{title.replace('*', '')}**\n\n{text}"
            chunks = [to_markdown_v2(c) for c in _chunks_markdown(text, max_chars)]
            parse_mode = "MarkdownV2"
        else:
            chunks = _chunks_by_line(text, max_chars)
        for chunk in chunks:
            try:
                mid = await send(str(chat_id), chunk, parse_mode)
            except Exception as exc:
                if parse_mode is None:
                    raise
                # Telegram rejected the markup. These are cron alerts, so
                # delivering the text unformatted beats dropping it. Strip the
                # markers first: resending raw MarkdownV2 would show the reader
                # backslashes in front of every full stop.
                logger.warning("formatted push rejected, retrying plain: %s", exc)
                mid = await send(str(chat_id), strip_markers(chunk), None)
            ids.append(mid)
        return web.json_response({"ok": True, "message_ids": ids})

    app = web.Application()
    app.router.add_post("/push", handle_push)
    return app
