"""Markdown -> Telegram MarkdownV2 conversion.

Matches the convention the other bots use (Hermes' telegram adapter): convert
standard markdown to MarkdownV2, protect code spans so escaping cannot corrupt
their contents, and keep a plain-text fallback for when Telegram rejects the
markup anyway.

The tests that matter here are the escaping ones. Telegram rejects a message
whose MarkdownV2 is malformed, so an unescaped '.' or '-' in ordinary prose is
not a cosmetic bug -- it fails the whole send.
"""
import pytest

from claude_telegram.richtext import strip_markers, to_markdown_v2


# --- escaping ---------------------------------------------------------------

def test_escapes_reserved_punctuation_in_prose():
    # A bare '.' or '!' is enough for Telegram to reject the message.
    assert to_markdown_v2("Done. Really!") == "Done\\. Really\\!"


def test_escapes_hyphens_and_parens():
    assert to_markdown_v2("well-known (yes)") == "well\\-known \\(yes\\)"


def test_escapes_backslash():
    assert to_markdown_v2("a\\b") == "a\\\\b"


def test_plain_text_with_no_specials_is_unchanged():
    assert to_markdown_v2("all quiet today") == "all quiet today"


def test_empty_input_survives():
    assert to_markdown_v2("") == ""


# --- constructs -------------------------------------------------------------

def test_bold_becomes_single_asterisk():
    assert to_markdown_v2("**loud**") == "*loud*"


def test_italic_becomes_underscore():
    assert to_markdown_v2("_soft_") == "_soft_"


def test_heading_becomes_bold():
    # MarkdownV2 has no headings; the other bots render them bold.
    assert to_markdown_v2("## Summary") == "*Summary*"


def test_link_is_preserved():
    assert to_markdown_v2("[docs](https://x.com/a_b)") == "[docs](https://x.com/a_b)"


# --- code spans are protected from escaping ---------------------------------

def test_inline_code_contents_are_not_escaped():
    # 'a.b-c' inside backticks must stay literal, not become 'a\.b\-c'.
    assert to_markdown_v2("`a.b-c`") == "`a.b-c`"


def test_fenced_block_contents_are_not_escaped():
    out = to_markdown_v2("```\nrm -rf /tmp/x.log\n```")
    assert "rm -rf /tmp/x.log" in out
    assert "\\-" not in out


def test_fenced_block_keeps_its_language():
    assert to_markdown_v2("```python\nx = 1\n```").startswith("```python")


def test_backtick_inside_fence_is_escaped_per_spec():
    assert "\\`" in to_markdown_v2("```\na ` b\n```")


def test_prose_outside_a_fence_is_still_escaped():
    out = to_markdown_v2("see `x.y` now.")
    assert out.endswith("now\\.")
    assert "`x.y`" in out


# --- the fallback -----------------------------------------------------------

def test_strip_markers_removes_escapes():
    assert strip_markers("Done\\. Really\\!") == "Done. Really!"


def test_strip_markers_removes_bold_markers():
    assert strip_markers("*loud*") == "loud"


def test_strip_markers_leaves_snake_case_intact():
    # Underscore stripping must not eat identifiers like my_var_name.
    assert strip_markers("my_var_name") == "my_var_name"


def test_strip_markers_output_has_no_stray_syntax():
    # Backticks are left alone: they read fine unformatted, and stripping them
    # eats one of the three in a ``` fence, corrupting the block.
    assert strip_markers(to_markdown_v2("**bold** and `code.x`")) == "bold and `code.x`"


def test_strip_markers_leaves_a_fenced_block_intact():
    out = strip_markers(to_markdown_v2("```bash\nls -l\n```"))
    assert out.startswith("```bash"), out
    assert out.rstrip().endswith("```"), out


def test_heading_keeps_the_blank_line_after_it():
    # A greedy trailing \s* swallows the paragraph break, gluing the heading
    # to the body.
    assert to_markdown_v2("## Title\n\nBody") == "*Title*\n\nBody"


# --- totality: never raise, whatever the stream hands us --------------------

@pytest.mark.parametrize("chunk", [
    "**unclosed bold",
    "`unclosed code",
    "```\nunclosed fence",
    "[unclosed link](",
    "###",
    "\\",
    "|table|",
])
def test_partial_markdown_never_raises(chunk):
    # Streamed text arrives truncated at arbitrary points.
    assert isinstance(to_markdown_v2(chunk), str)


# --- nesting: one construct stashed inside another --------------------------
# Placeholders must restore in reverse insertion order, or an inner placeholder
# left inside an outer one is never resolved and raw NUL bytes reach Telegram.

@pytest.mark.parametrize("src", [
    "**see [docs](https://x.com)**",
    "[`a.b`](https://x.com)",
    "## See [docs](https://x.com)",
    "~~[gone](https://x.com)~~",
    "**bold with `code` inside**",
])
def test_nested_constructs_leave_no_placeholder_bytes(src):
    assert "\x00" not in to_markdown_v2(src)


def test_link_inside_bold_survives():
    assert to_markdown_v2("**see [docs](https://x.com)**") == \
        "*see [docs](https://x.com)*"


def test_inline_code_inside_link_survives():
    assert to_markdown_v2("[`a.b`](https://x.com)") == "[`a.b`](https://x.com)"


# --- link targets -----------------------------------------------------------

def _url_of(markdown_link: str) -> str:
    """Pull the target out of `[text](url)` and undo MarkdownV2 escaping."""
    import re
    body = markdown_link[markdown_link.index("](") + 2:-1]
    return re.sub(r"\\(.)", r"\1", body)


def test_url_containing_parentheses_is_kept_whole():
    # The inner ')' must be escaped (else it closes the entity early), but the
    # target must still round-trip to the original URL.
    out = to_markdown_v2("[x](https://en.wikipedia.org/wiki/Foo_(bar))")
    assert _url_of(out) == "https://en.wikipedia.org/wiki/Foo_(bar)"


def test_url_ending_in_backslash_still_closes_the_entity():
    # A RAW trailing backslash would escape the ')' that closes the link and
    # Telegram would reject the message; it must be escaped to '\\\\'.
    out = to_markdown_v2("[x](https://a.com/b\\)")
    trailing = len(out) - len(out[:-1].rstrip("\\")) - 1
    assert trailing % 2 == 0, f"odd backslash run before ')': {out!r}"
    assert _url_of(out) == "https://a.com/b\\"


# --- fallback must not touch protected code ---------------------------------

def test_strip_markers_preserves_operators_inside_a_fence():
    src = "```py\nx = a * b * c\n```"
    assert "a * b * c" in strip_markers(to_markdown_v2(src))


def test_strip_markers_preserves_tilde_paths_inside_a_fence():
    src = "```\ncd ~/proj and ~/other\n```"
    assert "~/proj and ~/other" in strip_markers(to_markdown_v2(src))


# --- misc -------------------------------------------------------------------

def test_heading_with_crlf_does_not_trap_the_carriage_return():
    assert to_markdown_v2("## Title\r\nBody").startswith("*Title*")


def test_literal_placeholder_bytes_in_input_cannot_hijack_a_fragment():
    # Input containing our sentinel must not be substituted with other content.
    out = to_markdown_v2("x \x00P0\x00 y **b**")
    assert out.endswith("*b*")
    assert "\x00P0\x00" not in out or out.count("*b*") == 1
