import pytest
from claude_telegram.stream import StreamRenderer, MessageSink
from claude_telegram.runner import RunnerEvent


class FakeSink(MessageSink):
    def __init__(self):
        self.calls: list[tuple] = []
        self._next_id = 100

    async def send(self, text: str) -> int:
        self._next_id += 1
        self.calls.append(("send", self._next_id, text))
        return self._next_id

    async def edit(self, message_id: int, text: str) -> None:
        self.calls.append(("edit", message_id, text))


@pytest.mark.asyncio
async def test_streams_text_in_one_message_under_limit():
    sink = FakeSink()
    t = [0.0]
    r = StreamRenderer(sink, max_chars=100, min_edit_interval_s=1.0, now_fn=lambda: t[0])
    await r.start_placeholder()
    t[0] = 2.0  # past edit interval
    await r.handle(RunnerEvent("text", {"text": "Hello "}))
    await r.handle(RunnerEvent("text", {"text": "world"}))
    t[0] = 4.0
    await r.handle(RunnerEvent("result", {"cost_usd": 0.001, "session_id": "s"}))
    await r.finalize()
    # First call is the placeholder send. Last edit should include both text chunks + ✅ marker.
    assert sink.calls[0][0] == "send"
    last = sink.calls[-1]
    assert last[0] == "edit"
    assert "Hello world" in last[2]
    assert "✅" in last[2]


@pytest.mark.asyncio
async def test_splits_at_char_limit():
    sink = FakeSink()
    t = [0.0]
    r = StreamRenderer(sink, max_chars=20, min_edit_interval_s=0.0, now_fn=lambda: t[0])
    await r.start_placeholder()
    await r.handle(RunnerEvent("text", {"text": "A" * 15}))
    await r.handle(RunnerEvent("text", {"text": "B" * 15}))  # overflows; should split
    await r.finalize()
    sends = [c for c in sink.calls if c[0] == "send"]
    # placeholder + at least one overflow message
    assert len(sends) >= 2


@pytest.mark.asyncio
async def test_renders_tool_use_inline():
    sink = FakeSink()
    r = StreamRenderer(sink, max_chars=4000, min_edit_interval_s=0.0, now_fn=lambda: 0.0)
    await r.start_placeholder()
    await r.handle(RunnerEvent("tool_use", {"name": "Bash", "input": {"command": "ls /tmp"}}))
    await r.finalize()
    final = sink.calls[-1]
    assert "🔧 Bash" in final[2]
    assert "ls /tmp" in final[2]


@pytest.mark.asyncio
async def test_respects_min_edit_interval():
    sink = FakeSink()
    t = [0.0]
    r = StreamRenderer(sink, max_chars=4000, min_edit_interval_s=1.0, now_fn=lambda: t[0])
    await r.start_placeholder()
    # immediate edits should NOT fire
    await r.handle(RunnerEvent("text", {"text": "a"}))
    await r.handle(RunnerEvent("text", {"text": "b"}))
    edits = [c for c in sink.calls if c[0] == "edit"]
    assert edits == []
    # advance time past interval; finalize forces a flush
    t[0] = 2.0
    await r.finalize()
    edits = [c for c in sink.calls if c[0] == "edit"]
    assert len(edits) >= 1
