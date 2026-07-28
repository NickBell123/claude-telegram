import asyncio
import json
import sys
import textwrap
from pathlib import Path

import pytest

from claude_telegram.runner import ClaudeRunner, RunnerEvent


def _fake_claude_script(tmp_path: Path, events: list[dict]) -> Path:
    """Write a python script that mimics `claude -p --output-format stream-json`."""
    script = tmp_path / "fake_claude.py"
    script.write_text(textwrap.dedent(f"""
        import json, sys, time
        for ev in {events!r}:
            print(json.dumps(ev), flush=True)
        """).strip())
    return script


@pytest.mark.asyncio
async def test_yields_session_id_text_and_result(tmp_path: Path):
    events = [
        {"type": "system", "subtype": "init", "session_id": "sess-1", "cwd": "/work/proj"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.0042, "session_id": "sess-1"},
    ]
    script = _fake_claude_script(tmp_path, events)
    runner = ClaudeRunner(claude_cmd=[sys.executable, str(script)])
    out = []
    async for ev in runner.run(prompt="hi", session_id=None, cwd=str(tmp_path)):
        out.append(ev)
    kinds = [e.kind for e in out]
    assert kinds == ["session", "text", "tool_use", "result"]
    assert out[0].data["session_id"] == "sess-1"
    assert out[1].data["text"] == "Hello"
    assert out[2].data == {"name": "Bash", "input": {"command": "ls"}}
    assert out[3].data["cost_usd"] == 0.0042


@pytest.mark.asyncio
async def test_passes_resume_flag(tmp_path: Path):
    """When session_id provided, --resume is in argv."""
    script = tmp_path / "echo_argv.py"
    script.write_text(textwrap.dedent("""
        import json, sys
        print(json.dumps({"type": "result", "subtype": "success", "total_cost_usd": 0.0, "session_id": "x", "argv": sys.argv}), flush=True)
        """).strip())
    runner = ClaudeRunner(claude_cmd=[sys.executable, str(script)])
    out = []
    async for ev in runner.run(prompt="hi", session_id="abc", cwd="/tmp"):
        out.append(ev)
    # last event is result, with argv embedded
    argv = out[-1].data["raw"]["argv"]
    assert "--resume" in argv
    assert "abc" in argv
