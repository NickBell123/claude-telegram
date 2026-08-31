"""Standard markdown -> Telegram MarkdownV2, with a plain-text fallback.

Matches the convention used by the other Telegram bots on this machine (the
Hermes adapter): MarkdownV2 rather than HTML, code spans protected behind
placeholders so escaping cannot corrupt their contents, and a strip function
for the fallback path when Telegram rejects the markup anyway.

MarkdownV2 is unforgiving: a single unescaped '.' or '-' in ordinary prose
makes Telegram reject the entire message. So the rule here is escape
everything by default, and carve out only the constructs we deliberately
support.
"""
import re
import uuid

# Every character MarkdownV2 requires to be backslash-escaped outside a code
# span. Kept identical to the Hermes adapter's set so both bots agree.
_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#\+\-=|{}.!\\])")

_FENCE_RE = re.compile(r"(```(?:[^\n]*\n)?[\s\S]*?```)")
_INLINE_CODE_RE = re.compile(r"(`[^`\n]+`)")
_BOLD_RE = re.compile(r"\*\*([^*\n]+)\*\*")
_ITALIC_RE = re.compile(r"(?<!\w)_([^_\n]+)_(?!\w)")
_STRIKE_RE = re.compile(r"~~([^~\n]+)~~")
_HEADING_RE = re.compile(r"(?m)^[ \t]{0,3}#{1,6}[ \t]+(.+?)[ \t\r]*$")
_LINK_RE = re.compile(r"\[([^\]\n]*)\]\(([^()\s]*(?:\([^()\s]*\)[^()\s]*)*)\)")


def _escape(text: str) -> str:
    return _ESCAPE_RE.sub(r"\\\1", text)


def to_markdown_v2(text: str) -> str:
    """Convert standard markdown to Telegram MarkdownV2.

    Total by construction: any string in, a valid string out. Streamed text
    arrives truncated at arbitrary points, so unclosed constructs simply fail
    to match their pattern and fall through to plain escaped text rather than
    raising or emitting half a tag.
    """
    if not text:
        return text

    stash: dict[str, str] = {}
    # Random per-call sentinel: a fixed token could appear in the input itself
    # and hijack another fragment's content on restore.
    nonce = uuid.uuid4().hex

    def _hold(value: str) -> str:
        """Park a finished fragment behind a token that survives escaping."""
        key = f"\x00{nonce}{len(stash)}\x00"
        stash[key] = value
        return key

    # 1) Protect code first — nothing inside it may be escaped or converted.
    #    Per the MarkdownV2 spec, backslash and backtick inside a code span do
    #    still need escaping, and only those two.
    def _fence(m: re.Match) -> str:
        raw = m.group(0)
        head = raw.index("\n") + 1 if "\n" in raw[3:] else 3
        body = raw[head:-3].replace("\\", "\\\\").replace("`", "\\`")
        return _hold(raw[:head] + body + "```")

    text = _FENCE_RE.sub(_fence, text)

    def _inline(m: re.Match) -> str:
        body = m.group(0)[1:-1].replace("\\", "\\\\").replace("`", "\\`")
        return _hold(f"`{body}`")

    text = _INLINE_CODE_RE.sub(_inline, text)

    # 2) Convert the constructs we support into placeholders holding their
    #    finished MarkdownV2, with the *inner* text escaped.
    def _link(m: re.Match) -> str:
        url = m.group(2).replace("\\", "\\\\").replace(")", "\\)")
        return _hold(f"[{_escape(m.group(1))}]({url})")

    text = _LINK_RE.sub(_link, text)
    text = _HEADING_RE.sub(
        lambda m: _hold(f"*{_escape(m.group(1))}*"), text)
    text = _BOLD_RE.sub(lambda m: _hold(f"*{_escape(m.group(1))}*"), text)
    text = _STRIKE_RE.sub(lambda m: _hold(f"~{_escape(m.group(1))}~"), text)
    text = _ITALIC_RE.sub(lambda m: _hold(f"_{_escape(m.group(1))}_"), text)

    # 3) Everything left is prose: escape it wholesale, then restore.
    text = _escape(text)
    # Reverse insertion order: an inner fragment is stashed before the outer one
    # that contains it, so the outer must be restored first for the inner token
    # to reappear in the text and be replaced in turn. Forward order leaves the
    # inner token embedded in the outer value and ships raw NUL bytes.
    for key in reversed(list(stash)):
        text = text.replace(_escape(key), stash[key]).replace(key, stash[key])
    return text


def strip_markers(text: str) -> str:
    """Reduce MarkdownV2 back to clean plain text for the fallback path.

    Used when Telegram rejects the formatted message: resending the raw
    MarkdownV2 unformatted would show the reader stray backslashes and
    asterisks, which is worse than the plain text we started with.
    """
    if not text:
        return text

    def _clean(chunk: str) -> str:
        out = re.sub(r"\\([_*\[\]()~`>#\+\-=|{}.!\\])", r"\1", chunk)
        out = re.sub(r"\*\*([^*]+)\*\*", r"\1", out)
        out = re.sub(r"\*([^*]+)\*", r"\1", out)
        out = re.sub(r"(?<!\w)_([^_]+)_(?!\w)", r"\1", out)
        out = re.sub(r"~([^~]+)~", r"\1", out)
        # Backticks are deliberately left in place. Stripping inline code would
        # also consume one backtick of a ``` fence and corrupt the block, and a
        # backtick reads perfectly well as plain text.
        return out

    # Only clean OUTSIDE code spans. The marker passes are not code-aware, so
    # run over a fence body they eat multiplication operators (a * b * c) and
    # home-dir tildes (~/proj) — silently wrong code, on the very path that
    # exists because something already went wrong.
    parts = re.split(r"(```[\s\S]*?```|`[^`\n]+`)", text)
    return "".join(part if i % 2 else _clean(part)
                   for i, part in enumerate(parts))
