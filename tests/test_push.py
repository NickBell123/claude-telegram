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
