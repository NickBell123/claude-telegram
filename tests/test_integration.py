import json
import sys
import textwrap
from pathlib import Path

import pytest

from claude_telegram.runner import ClaudeRunner
from claude_telegram.stream import StreamRenderer, MessageSink


class FakeSink(MessageSink):
    def __init__(self):
        self.calls: list[tuple] = []
        self._id = 0

    async def send(self, text: str) -> int:
        self._id += 1
        self.calls.append(("send", self._id, text))
        return self._id

    async def edit(self, message_id: int, text: str, finalize: bool = False) -> None:
        self.calls.append(("edit", message_id, text))


@pytest.mark.asyncio
async def test_runner_to_renderer_end_to_end(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "s1", "cwd": "/tmp"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Looking…"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "echo hi"}}]}},
        {"type": "user", "message": {"content": [{"type": "tool_result", "content": "hi", "is_error": False}]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": " done."}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.0001, "session_id": "s1"},
    ]
    script = tmp_path / "fake.py"
    script.write_text("import json\n" + "\n".join(f"print(json.dumps({e!r}), flush=True)" for e in events))
    runner = ClaudeRunner(claude_cmd=[sys.executable, str(script)])
    sink = FakeSink()
    renderer = StreamRenderer(sink, max_chars=4000, min_edit_interval_s=0.0, now_fn=lambda: 0.0)
    await renderer.start_placeholder()
    async for ev in runner.run(prompt="x", session_id=None, cwd="/tmp"):
        await renderer.handle(ev)
    await renderer.finalize()
    final = sink.calls[-1]
    assert final[0] == "edit"
    body = final[2]
    assert "Looking…" in body
    assert "\U0001f527 Bash" in body
    assert "echo hi" in body
    assert " done." in body
    assert "✅" in body
    assert "$0.0001" in body
