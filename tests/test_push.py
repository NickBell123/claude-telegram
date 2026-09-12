import pytest
from aiohttp.test_utils import TestClient, TestServer
from claude_telegram.push import build_app


@pytest.fixture
async def client():
    sent: list[tuple[str, str]] = []
    modes: list[str | None] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        sent.append((chat_id, text))
        modes.append(parse_mode)
        return len(sent)

    app = build_app(push_token="secret", default_chat_id="42", send=fake_send, max_chars=20)
    async with TestClient(TestServer(app)) as c:
        c.sent = sent  # type: ignore[attr-defined]
        c.modes = modes  # type: ignore[attr-defined]
        yield c


@pytest.mark.asyncio
async def test_rejects_missing_auth(client):
    r = await client.post("/push", json={"text": "hi"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_rejects_wrong_token(client):
    r = await client.post("/push", json={"text": "hi"}, headers={"Authorization": "Bearer wrong"})
    assert r.status == 401


@pytest.mark.asyncio
async def test_accepts_and_sends(client):
    r = await client.post("/push", json={"text": "hi"}, headers={"Authorization": "Bearer secret"})
    assert r.status == 200
    body = await r.json()
    assert body["ok"] is True
    assert len(body["message_ids"]) == 1
    assert client.sent == [("42", "hi")]


@pytest.mark.asyncio
async def test_overrides_chat_id(client):
    r = await client.post(
        "/push",
        json={"text": "hi", "chat_id": "99"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert client.sent == [("99", "hi")]


@pytest.mark.asyncio
async def test_chunks_long_text(client):
    long = "A" * 50  # max_chars=20 → 3 chunks
    r = await client.post(
        "/push",
        json={"text": long},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    body = await r.json()
    assert len(body["message_ids"]) == 3
    assert "".join(t for _, t in client.sent) == long


@pytest.mark.asyncio
async def test_forwards_parse_mode_to_send(client):
    r = await client.post(
        "/push",
        json={"text": "*hi*", "parse_mode": "MarkdownV2"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert client.modes == ["MarkdownV2"]


@pytest.mark.asyncio
async def test_rejects_unknown_parse_mode(client):
    r = await client.post(
        "/push",
        json={"text": "hi", "parse_mode": "Markdownv3"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 400
    assert client.sent == [], "must not send anything Telegram will reject"


@pytest.mark.asyncio
async def test_chunks_at_line_boundaries_when_formatting(client):
    # 21 chars with max_chars=20: a blind character split lands inside the
    # second bold run, and Telegram rejects both halves as malformed.
    r = await client.post(
        "/push",
        json={"text": "*aaaaaaaa*\n*bbbbbbbb*", "parse_mode": "MarkdownV2"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert [t for _, t in client.sent] == ["*aaaaaaaa*", "*bbbbbbbb*"]


@pytest.mark.asyncio
async def test_hard_splits_a_single_line_too_long_to_fit(client):
    # No line boundary to use; splitting is still better than dropping text.
    long = "A" * 50
    r = await client.post(
        "/push",
        json={"text": long, "parse_mode": "MarkdownV2"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert "".join(t for _, t in client.sent) == long


# --- markdown conversion on the push path ---------------------------------
# The cron alerts (morning brief, Pionex run summary) are LLM-generated
# GitHub-flavoured markdown. Before this, tg-push HTML-escaped them wholesale,
# so every '**bold**' arrived as literal asterisks. The endpoint now owns the
# conversion, using the same richtext converter as the chat path.


@pytest.mark.asyncio
async def test_markdown_flag_converts_bold_and_sets_markdownv2(client):
    r = await client.post(
        "/push",
        json={"text": "**loud**", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert [t for _, t in client.sent] == ["*loud*"]
    assert client.modes == ["MarkdownV2"]


@pytest.mark.asyncio
async def test_markdown_flag_escapes_reserved_prose_characters(client):
    r = await client.post(
        "/push",
        json={"text": "up 2.5%", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert [t for _, t in client.sent] == ["up 2\\.5%"]


@pytest.mark.asyncio
async def test_markdown_flag_rejects_an_explicit_parse_mode(client):
    # Converting text the caller already formatted would double-escape it.
    r = await client.post(
        "/push",
        json={"text": "*hi*", "markdown": True, "parse_mode": "MarkdownV2"},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 400


@pytest.mark.asyncio
async def test_markdown_chunks_stay_within_the_limit_after_escaping(client):
    # max_chars=20. Escaping inflates: 16 raw dots become 32 characters, so
    # chunking the raw text and converting afterwards overflows the limit
    # unless the packer measures the CONVERTED length.
    r = await client.post(
        "/push",
        json={"text": "\n".join("a." * 5 for _ in range(6)), "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert client.sent, "nothing sent"
    for _, t in client.sent:
        assert len(t) <= 20, f"chunk of {len(t)} exceeds the limit: {t!r}"


@pytest.mark.asyncio
async def test_markdown_never_splits_an_escape_sequence(client):
    # A backslash orphaned at a chunk boundary makes Telegram reject the chunk.
    r = await client.post(
        "/push",
        json={"text": "x" * 30 + ". done.", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    for _, t in client.sent:
        assert not t.endswith("\\") or t.endswith("\\\\"), f"dangling escape: {t!r}"


# The exact block Nick received on his phone with literal '**' markers.
NICKS_SCORECARD = """**📋 Setups scorecard**
- Nothing resolved in the last 24h.
- Win-rate **31%** (83W / 187L, 28 open).
- New today: **PLTR long** (7W/7L — a coin flip with extra steps 🎲)"""


@pytest.mark.asyncio
async def test_real_brief_block_renders_bold_not_literal_asterisks():
    sent: list[str] = []
    modes: list[str | None] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        sent.append(text)
        modes.append(parse_mode)
        return 1

    app = build_app(push_token="s", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/push",
            json={"text": NICKS_SCORECARD, "markdown": True},
            headers={"Authorization": "Bearer s"},
        )
        assert r.status == 200

    assert len(sent) == 1
    out = sent[0]
    assert modes == ["MarkdownV2"]
    # No GFM double-asterisk survives to the reader.
    assert "**" not in out
    # All three bolded spans became real MarkdownV2 bold.
    assert "*📋 Setups scorecard*" in out
    assert "*31%*" in out
    assert "*PLTR long*" in out
    # Bullets survive, escaped so MarkdownV2 accepts them.
    assert out.count("\n\\- ") == 3 and out.startswith("*📋")
    # Reserved prose characters are escaped.
    assert "24h\\." in out and "\\(83W / 187L, 28 open\\)" in out
    # Emoji and the em-dash pass through untouched.
    assert "🎲" in out and "—" in out


@pytest.mark.asyncio
async def test_outer_markdown_fence_from_the_model_is_unwrapped():
    # Models sometimes wrap a whole generated brief in a ```markdown fence.
    # Taken literally that turns the entire alert into one monospace block
    # with every '**' showing. A fence tagged 'markdown' that encloses the
    # whole message is an artifact of generation, not content.
    sent: list[str] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        sent.append(text)
        return 1

    app = build_app(push_token="s", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/push",
            json={
                "text": "```markdown\n" + NICKS_SCORECARD + "\n```",
                "markdown": True,
                "title": "🌅 Morning Brief",
            },
            headers={"Authorization": "Bearer s"},
        )
        assert r.status == 200

    out = sent[0]
    assert "```" not in out
    assert "**" not in out
    assert out.startswith("*🌅 Morning Brief*\n\n")
    assert "*PLTR long*" in out


@pytest.mark.asyncio
async def test_a_genuine_code_fence_is_left_alone(client):
    # Only a whole-message 'markdown'-tagged fence is an artifact. A real code
    # block must keep its monospace rendering.
    r = await client.post(
        "/push",
        json={"text": "```python\nx = 1\n```", "markdown": True},
        headers={"Authorization": "Bearer secret"},
    )
    assert r.status == 200
    assert "".join(t for _, t in client.sent).startswith("```python")


@pytest.mark.asyncio
async def test_title_is_bolded_and_escaped_by_the_server():
    sent: list[str] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        sent.append(text)
        return 1

    app = build_app(push_token="s", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/push",
            json={"text": "body", "markdown": True, "title": "Run (exit=0) 2.5%"},
            headers={"Authorization": "Bearer s"},
        )
        assert r.status == 200
    assert sent[0] == "*Run \\(exit\\=0\\) 2\\.5%*\n\nbody"


@pytest.mark.asyncio
async def test_a_rejected_conversion_falls_back_to_plain_text():
    # The old HTML path could never be rejected, so an alert always arrived.
    # Converting reintroduces the possibility of a 400, and a dropped Pionex
    # or backup alert is far worse than an unformatted one.
    attempts: list[tuple[str, str | None]] = []

    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        attempts.append((text, parse_mode))
        if parse_mode is not None:
            raise RuntimeError("Bad Request: can't parse entities")
        return 1

    app = build_app(push_token="s", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/push",
            json={"text": "**loud** and 2.5%", "markdown": True},
            headers={"Authorization": "Bearer s"},
        )
        assert r.status == 200
        assert (await r.json())["ok"] is True

    assert [m for _, m in attempts] == ["MarkdownV2", None]
    # The fallback must not show the reader escaping artefacts.
    assert attempts[1][0] == "loud and 2.5%"


@pytest.mark.asyncio
async def test_a_plain_send_failure_still_surfaces():
    async def fake_send(chat_id: str, text: str, parse_mode: str | None = None) -> int:
        raise RuntimeError("network down")

    app = build_app(push_token="s", default_chat_id="42", send=fake_send, max_chars=4000)
    async with TestClient(TestServer(app)) as c:
        r = await c.post(
            "/push",
            json={"text": "hi", "markdown": True},
            headers={"Authorization": "Bearer s"},
        )
        assert r.status >= 500
