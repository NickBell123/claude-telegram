import hmac
from typing import Awaitable, Callable, Optional

from aiohttp import web

SendFn = Callable[[str, str, Optional[str]], Awaitable[int]]

# Telegram rejects anything else with an opaque 400. Validate here so a cron job
# gets a useful error instead of a 500 from a failed send.
PARSE_MODES = {"MarkdownV2", "HTML", "Markdown"}


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
        ids = []
        for chunk in _chunks_by_line(text, max_chars):
            mid = await send(str(chat_id), chunk, parse_mode)
            ids.append(mid)
        return web.json_response({"ok": True, "message_ids": ids})

    app = web.Application()
    app.router.add_post("/push", handle_push)
    return app
