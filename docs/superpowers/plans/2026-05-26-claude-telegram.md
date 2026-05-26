# claude-telegram Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Telegram bot on the Aspire host that bridges Telegram messages to the `claude` CLI (full Claude Code parity) and exposes a localhost HTTP endpoint for cron jobs to push proactive messages.

**Architecture:** Single Python asyncio process. `python-telegram-bot` long-polls Telegram and gates updates to a single allowed user ID. Each message spawns `claude -p --resume <session_id> --output-format stream-json --dangerously-skip-permissions` as a subprocess; stdout is parsed line-by-line and rendered to Telegram by editing one placeholder message live (≤1 edit/1.2s, split at 3900 chars). An `aiohttp` server on `127.0.0.1:8787` accepts bearer-token-authenticated POSTs and forwards them as Telegram messages. State persists at `~/.claude-telegram/`. Deployed as a user systemd unit.

**Tech Stack:** Python 3.12, `python-telegram-bot >= 21`, `aiohttp`, `pytest` + `pytest-asyncio`, `claude` CLI, `systemd --user`.

**Spec:** `docs/superpowers/specs/2026-05-26-claude-telegram-design.md`

**File structure (all paths relative to `/home/nick/claude-telegram/`):**
```
claude_telegram/
  __init__.py
  __main__.py        # entrypoint: wires bot + push server + runner
  config.py          # env loading
  state.py           # ~/.claude-telegram/state.json read/write
  log.py             # append-only JSONL logger
  ratelimit.py       # per-chat sliding-window limiter
  runner.py          # ClaudeRunner: spawn claude subprocess, yield stream-json events
  stream.py          # StreamRenderer: turn events into Telegram message edits
  bot.py             # TelegramBot: handlers, auth gate, commands
  push.py            # PushServer: aiohttp /push endpoint
tests/
  test_config.py
  test_state.py
  test_ratelimit.py
  test_runner.py
  test_stream.py
  test_push.py
  test_bot_auth.py
  test_integration.py
scripts/
  tg-push            # CLI helper (Python)
  install.sh
systemd/
  claude-telegram.service
pyproject.toml
requirements.txt
.gitignore
README.md
```

Each module has one responsibility; modules talk via simple dataclasses/queues. No module references another's internals.

---

### Task 1: Project scaffold

**Files:**
- Create: `pyproject.toml`, `requirements.txt`, `.gitignore`, `claude_telegram/__init__.py`, `tests/__init__.py`

- [ ] **Step 1: Create `.gitignore`**

```
.venv/
__pycache__/
*.pyc
.pytest_cache/
*.egg-info/
.coverage
```

- [ ] **Step 2: Create `requirements.txt`**

```
python-telegram-bot>=21,<22
aiohttp>=3.9,<4
```

- [ ] **Step 3: Create `pyproject.toml`**

```toml
[project]
name = "claude-telegram"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 4: Create empty package files**

`claude_telegram/__init__.py`:
```python
"""Telegram bridge to the claude CLI."""
```

`tests/__init__.py`:
```python
```

- [ ] **Step 5: Create venv and install**

```bash
cd /home/nick/claude-telegram
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt pytest pytest-asyncio
```

Expected: pip installs without errors.

- [ ] **Step 6: Verify pytest finds the empty suite**

Run: `.venv/bin/pytest -q`
Expected: `no tests ran`.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "scaffold: project layout, deps, pytest config"
```

---

### Task 2: Config loader

**Files:**
- Create: `claude_telegram/config.py`
- Test: `tests/test_config.py`

- [ ] **Step 1: Write the failing test**

`tests/test_config.py`:
```python
import os
import pytest
from claude_telegram.config import Config, ConfigError


def test_loads_from_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    cfg = Config.from_env()
    assert cfg.telegram_bot_token == "tok"
    assert cfg.allowed_user_id == 42
    assert cfg.default_chat_id == "42"
    assert cfg.push_token == "secret"
    assert cfg.push_host == "127.0.0.1"
    assert cfg.push_port == 8787


def test_missing_required_raises(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.setenv("ALLOWED_USER_ID", "42")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    with pytest.raises(ConfigError, match="TELEGRAM_BOT_TOKEN"):
        Config.from_env()


def test_allowed_user_id_must_be_int(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
    monkeypatch.setenv("ALLOWED_USER_ID", "not-a-number")
    monkeypatch.setenv("DEFAULT_CHAT_ID", "42")
    monkeypatch.setenv("PUSH_TOKEN", "secret")
    with pytest.raises(ConfigError, match="ALLOWED_USER_ID"):
        Config.from_env()
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: ImportError — `claude_telegram.config` does not exist.

- [ ] **Step 3: Implement `claude_telegram/config.py`**

```python
import os
from dataclasses import dataclass


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str
    allowed_user_id: int
    default_chat_id: str
    push_token: str
    push_host: str = "127.0.0.1"
    push_port: int = 8787
    state_dir: str = os.path.expanduser("~/.claude-telegram")
    rate_limit_per_hour: int = 60
    edit_min_interval_s: float = 1.2
    max_telegram_chars: int = 3900

    @classmethod
    def from_env(cls) -> "Config":
        def req(name: str) -> str:
            v = os.environ.get(name)
            if not v:
                raise ConfigError(f"{name} is required")
            return v

        try:
            allowed = int(req("ALLOWED_USER_ID"))
        except ValueError as e:
            raise ConfigError(f"ALLOWED_USER_ID must be an integer: {e}")

        return cls(
            telegram_bot_token=req("TELEGRAM_BOT_TOKEN"),
            allowed_user_id=allowed,
            default_chat_id=req("DEFAULT_CHAT_ID"),
            push_token=req("PUSH_TOKEN"),
        )
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_config.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: config loader with env-var validation"
```

---

### Task 3: State persistence

**Files:**
- Create: `claude_telegram/state.py`
- Test: `tests/test_state.py`

- [ ] **Step 1: Write the failing test**

`tests/test_state.py`:
```python
from pathlib import Path
from claude_telegram.state import StateStore, ChatState


def test_set_and_get_roundtrip(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set("123", ChatState(session_id="abc", cwd="/home/nick"))
    again = StateStore(tmp_path / "state.json")
    assert again.get("123") == ChatState(session_id="abc", cwd="/home/nick")


def test_get_missing_returns_default_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json", default_cwd="/tmp")
    s = store.get("999")
    assert s.session_id is None
    assert s.cwd == "/tmp"


def test_clear_session_keeps_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set("123", ChatState(session_id="abc", cwd="/x"))
    store.clear_session("123")
    s = store.get("123")
    assert s.session_id is None
    assert s.cwd == "/x"


def test_set_cwd(tmp_path: Path):
    store = StateStore(tmp_path / "state.json")
    store.set_cwd("123", "/new")
    assert store.get("123").cwd == "/new"
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/state.py`**

```python
import json
import os
import tempfile
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class ChatState:
    session_id: Optional[str]
    cwd: str


class StateStore:
    def __init__(self, path: Path, default_cwd: str = "/home/nick"):
        self.path = Path(path)
        self.default_cwd = default_cwd
        self._data = self._load()

    def _load(self) -> dict:
        if not self.path.exists():
            return {"chats": {}}
        with self.path.open() as f:
            return json.load(f)

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state.", suffix=".json")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, self.path)
        except Exception:
            os.unlink(tmp)
            raise

    def get(self, chat_id: str) -> ChatState:
        d = self._data["chats"].get(chat_id)
        if d is None:
            return ChatState(session_id=None, cwd=self.default_cwd)
        return ChatState(session_id=d.get("session_id"), cwd=d.get("cwd", self.default_cwd))

    def set(self, chat_id: str, state: ChatState) -> None:
        self._data["chats"][chat_id] = asdict(state)
        self._save()

    def set_session(self, chat_id: str, session_id: str) -> None:
        current = self.get(chat_id)
        self.set(chat_id, ChatState(session_id=session_id, cwd=current.cwd))

    def set_cwd(self, chat_id: str, cwd: str) -> None:
        current = self.get(chat_id)
        self.set(chat_id, ChatState(session_id=current.session_id, cwd=cwd))

    def clear_session(self, chat_id: str) -> None:
        current = self.get(chat_id)
        self.set(chat_id, ChatState(session_id=None, cwd=current.cwd))
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_state.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: state store with atomic writes"
```

---

### Task 4: Rate limiter

**Files:**
- Create: `claude_telegram/ratelimit.py`
- Test: `tests/test_ratelimit.py`

- [ ] **Step 1: Write the failing test**

`tests/test_ratelimit.py`:
```python
from claude_telegram.ratelimit import SlidingWindowLimiter


def test_allows_up_to_limit():
    lim = SlidingWindowLimiter(max_events=3, window_seconds=60.0, now_fn=lambda: 1000.0)
    assert lim.allow("a") is True
    assert lim.allow("a") is True
    assert lim.allow("a") is True
    assert lim.allow("a") is False


def test_separate_keys_have_separate_quotas():
    lim = SlidingWindowLimiter(max_events=1, window_seconds=60.0, now_fn=lambda: 1.0)
    assert lim.allow("a") is True
    assert lim.allow("b") is True
    assert lim.allow("a") is False


def test_events_expire():
    t = [0.0]
    lim = SlidingWindowLimiter(max_events=2, window_seconds=10.0, now_fn=lambda: t[0])
    assert lim.allow("a") is True
    t[0] = 5.0
    assert lim.allow("a") is True
    assert lim.allow("a") is False
    t[0] = 11.0  # first event now outside window
    assert lim.allow("a") is True
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_ratelimit.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/ratelimit.py`**

```python
import time
from collections import defaultdict, deque
from typing import Callable, Deque, Dict


class SlidingWindowLimiter:
    def __init__(
        self,
        max_events: int,
        window_seconds: float,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.max_events = max_events
        self.window_seconds = window_seconds
        self.now_fn = now_fn
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self.now_fn()
        q = self._events[key]
        cutoff = now - self.window_seconds
        while q and q[0] < cutoff:
            q.popleft()
        if len(q) >= self.max_events:
            return False
        q.append(now)
        return True
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_ratelimit.py -v`
Expected: 3 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: sliding-window rate limiter"
```

---

### Task 5: JSONL logger

**Files:**
- Create: `claude_telegram/log.py`
- Test: `tests/test_log.py`

- [ ] **Step 1: Write the failing test**

`tests/test_log.py`:
```python
import json
from pathlib import Path
from claude_telegram.log import JsonlLogger


def test_appends_lines(tmp_path: Path):
    log = JsonlLogger(tmp_path / "log.jsonl", now_fn=lambda: "2026-01-01T00:00:00Z")
    log.write("inbound", chat_id="42", details={"text": "hi"})
    log.write("push", chat_id="42", details={"len": 5})
    lines = (tmp_path / "log.jsonl").read_text().strip().splitlines()
    assert len(lines) == 2
    e1 = json.loads(lines[0])
    assert e1 == {
        "ts": "2026-01-01T00:00:00Z",
        "kind": "inbound",
        "chat_id": "42",
        "details": {"text": "hi"},
    }
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_log.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/log.py`**

```python
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class JsonlLogger:
    def __init__(self, path: Path, now_fn: Callable[[], str] = _now_iso):
        self.path = Path(path)
        self.now_fn = now_fn

    def write(self, kind: str, chat_id: Optional[str] = None, details: Optional[dict[str, Any]] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": self.now_fn(),
            "kind": kind,
            "chat_id": chat_id,
            "details": details or {},
        }
        with self.path.open("a") as f:
            f.write(json.dumps(entry) + "\n")
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_log.py -v`
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: jsonl logger"
```

---

### Task 6: ClaudeRunner (subprocess + stream-json parsing)

**Files:**
- Create: `claude_telegram/runner.py`
- Test: `tests/test_runner.py`

The runner spawns the `claude` CLI and yields a small set of normalized events the rest of the system understands. We don't try to model every stream-json event — just the four we render.

Stream-json events from `claude -p --output-format stream-json` come as newline-delimited JSON. Relevant shapes:
- `{"type":"system","subtype":"init","session_id":"...","cwd":"...","tools":[...]}` — first event on new session.
- `{"type":"assistant","message":{"content":[{"type":"text","text":"..."},{"type":"tool_use","name":"Bash","input":{...}}]}}` — assistant turn (may have multiple).
- `{"type":"user","message":{"content":[{"type":"tool_result","content":"...","is_error":false}]}}` — tool result.
- `{"type":"result","subtype":"success","total_cost_usd":0.012,"usage":{"input_tokens":...,"output_tokens":...},"session_id":"..."}` — final.

- [ ] **Step 1: Write the failing test**

`tests/test_runner.py`:
```python
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
        {"type": "system", "subtype": "init", "session_id": "sess-1", "cwd": "/home/nick"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Hello"}]}},
        {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}},
        {"type": "result", "subtype": "success", "total_cost_usd": 0.0042, "session_id": "sess-1"},
    ]
    script = _fake_claude_script(tmp_path, events)
    runner = ClaudeRunner(claude_cmd=[sys.executable, str(script)])
    out = []
    async for ev in runner.run(prompt="hi", session_id=None, cwd="/home/nick"):
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/runner.py`**

```python
import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class RunnerEvent:
    kind: str  # "session" | "text" | "tool_use" | "tool_result" | "result" | "error"
    data: dict


class ClaudeRunner:
    """Spawns the claude CLI and yields normalized events from its stream-json output."""

    def __init__(self, claude_cmd: list[str] | None = None):
        # Allow override for tests; default is the real CLI.
        self.claude_cmd = claude_cmd or ["claude"]

    def _build_argv(self, prompt: str, session_id: Optional[str]) -> list[str]:
        argv = list(self.claude_cmd) + [
            "-p", prompt,
            "--output-format", "stream-json",
            "--verbose",
            "--dangerously-skip-permissions",
        ]
        if session_id:
            argv += ["--resume", session_id]
        return argv

    async def run(
        self,
        prompt: str,
        session_id: Optional[str],
        cwd: str,
    ) -> AsyncIterator[RunnerEvent]:
        argv = self._build_argv(prompt, session_id)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self._proc = proc
        try:
            assert proc.stdout is not None
            async for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    yield RunnerEvent(kind="error", data={"raw": line.decode("utf-8", "replace")})
                    continue
                for normalized in self._normalize(ev):
                    yield normalized
        finally:
            rc = await proc.wait()
            if rc != 0:
                err = (await proc.stderr.read()).decode("utf-8", "replace") if proc.stderr else ""
                yield RunnerEvent(kind="error", data={"returncode": rc, "stderr": err})

    def _normalize(self, ev: dict) -> list[RunnerEvent]:
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            return [RunnerEvent(kind="session", data={"session_id": ev.get("session_id"), "cwd": ev.get("cwd")})]
        if t == "assistant":
            out = []
            for block in ev.get("message", {}).get("content", []) or []:
                if block.get("type") == "text":
                    out.append(RunnerEvent(kind="text", data={"text": block.get("text", "")}))
                elif block.get("type") == "tool_use":
                    out.append(RunnerEvent(kind="tool_use", data={"name": block.get("name"), "input": block.get("input", {})}))
            return out
        if t == "user":
            out = []
            for block in ev.get("message", {}).get("content", []) or []:
                if block.get("type") == "tool_result":
                    out.append(RunnerEvent(kind="tool_result", data={"content": block.get("content"), "is_error": block.get("is_error", False)}))
            return out
        if t == "result":
            return [RunnerEvent(kind="result", data={
                "cost_usd": ev.get("total_cost_usd", 0.0),
                "session_id": ev.get("session_id"),
                "raw": ev,
            })]
        return []

    def terminate(self) -> None:
        proc = getattr(self, "_proc", None)
        if proc and proc.returncode is None:
            proc.terminate()
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_runner.py -v`
Expected: 2 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: ClaudeRunner subprocess wrapper with stream-json normalization"
```

---

### Task 7: StreamRenderer (events → Telegram message edits)

**Files:**
- Create: `claude_telegram/stream.py`
- Test: `tests/test_stream.py`

StreamRenderer is the only place that knows about Telegram's edit-rate and message-length constraints. It exposes a simple async API the bot drives. To keep it testable, it talks to a `Sink` protocol — the real implementation passes a sink backed by Telegram, tests pass a fake.

- [ ] **Step 1: Write the failing test**

`tests/test_stream.py`:
```python
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_stream.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/stream.py`**

```python
import time
from typing import Callable, Protocol

from claude_telegram.runner import RunnerEvent


class MessageSink(Protocol):
    async def send(self, text: str) -> int: ...
    async def edit(self, message_id: int, text: str) -> None: ...


PLACEHOLDER = "⚡ thinking…"


class StreamRenderer:
    """
    Buffers runner events and renders them to Telegram by editing one message,
    respecting Telegram's edit-rate limit and per-message char limit.
    """

    def __init__(
        self,
        sink: MessageSink,
        max_chars: int = 3900,
        min_edit_interval_s: float = 1.2,
        now_fn: Callable[[], float] = time.monotonic,
    ):
        self.sink = sink
        self.max_chars = max_chars
        self.min_edit_interval_s = min_edit_interval_s
        self.now_fn = now_fn
        self._current_msg_id: int | None = None
        self._current_text: str = ""
        self._last_edit_at: float = 0.0
        self._final_cost: float | None = None

    async def start_placeholder(self) -> None:
        self._current_msg_id = await self.sink.send(PLACEHOLDER)
        self._current_text = ""
        self._last_edit_at = self.now_fn()

    async def handle(self, ev: RunnerEvent) -> None:
        addition = self._format(ev)
        if not addition:
            if ev.kind == "result":
                self._final_cost = ev.data.get("cost_usd")
            return
        # If adding would overflow, finalize current message and start a fresh one.
        if len(self._current_text) + len(addition) > self.max_chars:
            await self._flush(force=True)
            self._current_msg_id = await self.sink.send(PLACEHOLDER)
            self._current_text = ""
            self._last_edit_at = self.now_fn()
        self._current_text += addition
        # Throttled edit.
        if self.now_fn() - self._last_edit_at >= self.min_edit_interval_s:
            await self._flush(force=False)

    async def finalize(self) -> None:
        suffix = "\n\n✅"
        if self._final_cost is not None:
            suffix += f" (${self._final_cost:.4f})"
        self._current_text += suffix
        await self._flush(force=True)

    async def _flush(self, force: bool) -> None:
        if self._current_msg_id is None:
            return
        text = self._current_text or PLACEHOLDER
        await self.sink.edit(self._current_msg_id, text)
        self._last_edit_at = self.now_fn()

    def _format(self, ev: RunnerEvent) -> str:
        if ev.kind == "text":
            return ev.data.get("text", "")
        if ev.kind == "tool_use":
            name = ev.data.get("name", "?")
            inp = ev.data.get("input", {})
            # Compact one-line render. Bash gets its command; everything else gets a short JSON.
            if name == "Bash" and "command" in inp:
                detail = inp["command"]
            else:
                detail = ", ".join(f"{k}={v!r}" for k, v in list(inp.items())[:2])
            return f"\n> 🔧 {name}: {detail}\n"
        if ev.kind == "tool_result":
            return ""  # too noisy to render; rely on next assistant text
        if ev.kind == "error":
            return f"\n⚠️ error: {ev.data}\n"
        return ""
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_stream.py -v`
Expected: 4 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: StreamRenderer with throttled edits and char-limit splitting"
```

---

### Task 8: PushServer

**Files:**
- Create: `claude_telegram/push.py`
- Test: `tests/test_push.py`

PushServer takes a `send` callback (so it doesn't import the bot) and exposes an aiohttp app. Tests use aiohttp's test client.

- [ ] **Step 1: Write the failing test**

`tests/test_push.py`:
```python
import pytest
from aiohttp.test_utils import TestClient, TestServer
from claude_telegram.push import build_app


@pytest.fixture
async def client():
    sent: list[tuple[str, str]] = []

    async def fake_send(chat_id: str, text: str) -> int:
        sent.append((chat_id, text))
        return len(sent)

    app = build_app(push_token="secret", default_chat_id="42", send=fake_send, max_chars=20)
    async with TestClient(TestServer(app)) as c:
        c.sent = sent  # type: ignore[attr-defined]
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
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_push.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/push.py`**

```python
from typing import Awaitable, Callable

from aiohttp import web

SendFn = Callable[[str, str], Awaitable[int]]


def _chunks(text: str, n: int) -> list[str]:
    if not text:
        return [""]
    return [text[i : i + n] for i in range(0, len(text), n)]


def build_app(
    push_token: str,
    default_chat_id: str,
    send: SendFn,
    max_chars: int = 4000,
) -> web.Application:
    async def handle_push(request: web.Request) -> web.Response:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {push_token}":
            return web.json_response({"ok": False, "error": "unauthorized"}, status=401)
        try:
            body = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)
        text = body.get("text")
        if not isinstance(text, str) or not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        chat_id = body.get("chat_id") or default_chat_id
        ids = []
        for chunk in _chunks(text, max_chars):
            mid = await send(str(chat_id), chunk)
            ids.append(mid)
        return web.json_response({"ok": True, "message_ids": ids})

    app = web.Application()
    app.router.add_post("/push", handle_push)
    return app
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_push.py -v`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: PushServer with bearer auth and chunking"
```

---

### Task 9: TelegramBot — auth gate + commands

**Files:**
- Create: `claude_telegram/bot.py`
- Test: `tests/test_bot_auth.py`

The bot module owns the `Application` from python-telegram-bot but exposes pure functions for the parts we want to test (auth gate, command dispatch). The wiring to `Application` is glue tested in the integration test.

- [ ] **Step 1: Write the failing test**

`tests/test_bot_auth.py`:
```python
import pytest
from claude_telegram.bot import is_authorized, parse_command


def test_authorized_user():
    assert is_authorized(user_id=42, allowed_user_id=42) is True


def test_unauthorized_user():
    assert is_authorized(user_id=99, allowed_user_id=42) is False


def test_unauthorized_when_none():
    assert is_authorized(user_id=None, allowed_user_id=42) is False


def test_parse_known_command():
    assert parse_command("/reset") == ("reset", "")
    assert parse_command("/cd /tmp") == ("cd", "/tmp")
    assert parse_command("/cd  /tmp/foo bar") == ("cd", "/tmp/foo bar")


def test_parse_non_command():
    assert parse_command("hello world") == (None, "hello world")


def test_parse_empty():
    assert parse_command("") == (None, "")
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_bot_auth.py -v`
Expected: ImportError.

- [ ] **Step 3: Implement `claude_telegram/bot.py`**

```python
import asyncio
from dataclasses import dataclass
from typing import Optional

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, ApplicationBuilder, ContextTypes, MessageHandler, filters

from claude_telegram.config import Config
from claude_telegram.log import JsonlLogger
from claude_telegram.ratelimit import SlidingWindowLimiter
from claude_telegram.runner import ClaudeRunner
from claude_telegram.state import StateStore
from claude_telegram.stream import MessageSink, StreamRenderer

KNOWN_COMMANDS = {"reset", "cd", "cwd", "cost", "stop"}


def is_authorized(user_id: Optional[int], allowed_user_id: int) -> bool:
    return user_id is not None and user_id == allowed_user_id


def parse_command(text: str) -> tuple[Optional[str], str]:
    """Return (command_name | None, remaining_text)."""
    if not text.startswith("/"):
        return (None, text)
    rest = text[1:]
    # split on first whitespace; preserve everything after
    parts = rest.split(None, 1)
    if not parts:
        return (None, "")
    cmd = parts[0]
    arg = parts[1] if len(parts) > 1 else ""
    if cmd not in KNOWN_COMMANDS:
        return (None, text)
    return (cmd, arg)


@dataclass
class LastTurn:
    cost: float | None = None


class TelegramSink(MessageSink):
    """MessageSink backed by python-telegram-bot."""

    def __init__(self, app: Application, chat_id: str):
        self.app = app
        self.chat_id = chat_id

    async def send(self, text: str) -> int:
        msg = await self.app.bot.send_message(chat_id=self.chat_id, text=text)
        return msg.message_id

    async def edit(self, message_id: int, text: str) -> None:
        try:
            await self.app.bot.edit_message_text(chat_id=self.chat_id, message_id=message_id, text=text)
        except Exception:
            # Edits can fail on identical text or rate limits; ignore.
            pass


class Bot:
    def __init__(
        self,
        config: Config,
        state: StateStore,
        runner: ClaudeRunner,
        logger: JsonlLogger,
        limiter: SlidingWindowLimiter,
    ):
        self.config = config
        self.state = state
        self.runner = runner
        self.logger = logger
        self.limiter = limiter
        self._in_flight: dict[str, asyncio.Lock] = {}
        self._last_turn: dict[str, LastTurn] = {}
        self._current_runner: dict[str, ClaudeRunner] = {}
        self.app: Application = ApplicationBuilder().token(config.telegram_bot_token).build()
        self.app.add_handler(MessageHandler(filters.TEXT, self._on_message))

    async def send(self, chat_id: str, text: str) -> int:
        msg = await self.app.bot.send_message(chat_id=chat_id, text=text)
        return msg.message_id

    def _lock_for(self, chat_id: str) -> asyncio.Lock:
        lock = self._in_flight.get(chat_id)
        if lock is None:
            lock = asyncio.Lock()
            self._in_flight[chat_id] = lock
        return lock

    async def _on_message(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = update.effective_user
        chat = update.effective_chat
        msg = update.effective_message
        if not user or not chat or not msg or not msg.text:
            return
        if not is_authorized(user.id, self.config.allowed_user_id):
            self.logger.write("drop", chat_id=str(chat.id), details={"reason": "unauthorized", "user_id": user.id})
            return
        chat_id = str(chat.id)
        if not self.limiter.allow(chat_id):
            self.logger.write("drop", chat_id=chat_id, details={"reason": "rate_limited"})
            await self.app.bot.send_message(chat_id=chat_id, text="⛔ rate limit hit; try again later")
            return
        self.logger.write("inbound", chat_id=chat_id, details={"text": msg.text})

        cmd, arg = parse_command(msg.text)
        if cmd is not None:
            await self._handle_command(chat_id, cmd, arg)
            return

        async with self._lock_for(chat_id):
            await self._run_turn(chat_id, msg.text)

    async def _handle_command(self, chat_id: str, cmd: str, arg: str) -> None:
        if cmd == "reset":
            self.state.clear_session(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text="🔄 session cleared")
        elif cmd == "cd":
            if not arg:
                await self.app.bot.send_message(chat_id=chat_id, text="usage: /cd <path>")
                return
            self.state.set_cwd(chat_id, arg)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 cwd set to {arg}")
        elif cmd == "cwd":
            s = self.state.get(chat_id)
            await self.app.bot.send_message(chat_id=chat_id, text=f"📁 {s.cwd}")
        elif cmd == "cost":
            last = self._last_turn.get(chat_id)
            if last and last.cost is not None:
                await self.app.bot.send_message(chat_id=chat_id, text=f"💰 last turn: ${last.cost:.4f}")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="no completed turn yet")
        elif cmd == "stop":
            r = self._current_runner.get(chat_id)
            if r:
                r.terminate()
                await self.app.bot.send_message(chat_id=chat_id, text="🛑 stopping")
            else:
                await self.app.bot.send_message(chat_id=chat_id, text="nothing running")

    async def _run_turn(self, chat_id: str, text: str) -> None:
        state = self.state.get(chat_id)
        sink = TelegramSink(self.app, chat_id)
        renderer = StreamRenderer(
            sink,
            max_chars=self.config.max_telegram_chars,
            min_edit_interval_s=self.config.edit_min_interval_s,
        )
        await renderer.start_placeholder()
        self._current_runner[chat_id] = self.runner
        cost = None
        captured_session = state.session_id
        try:
            async for ev in self.runner.run(prompt=text, session_id=state.session_id, cwd=state.cwd):
                if ev.kind == "session" and ev.data.get("session_id"):
                    captured_session = ev.data["session_id"]
                if ev.kind == "result":
                    cost = ev.data.get("cost_usd")
                    if ev.data.get("session_id"):
                        captured_session = ev.data["session_id"]
                await renderer.handle(ev)
            await renderer.finalize()
        finally:
            self._current_runner.pop(chat_id, None)
        if captured_session and captured_session != state.session_id:
            self.state.set_session(chat_id, captured_session)
        self._last_turn[chat_id] = LastTurn(cost=cost)
        self.logger.write("claude", chat_id=chat_id, details={"cost_usd": cost, "session_id": captured_session})
```

- [ ] **Step 4: Verify pass**

Run: `.venv/bin/pytest tests/test_bot_auth.py -v`
Expected: 6 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: TelegramBot with auth gate, commands, and run loop"
```

---

### Task 10: Entry point wiring

**Files:**
- Create: `claude_telegram/__main__.py`

- [ ] **Step 1: Implement `claude_telegram/__main__.py`**

```python
import asyncio
import logging
import os
from pathlib import Path

from aiohttp import web

from claude_telegram.bot import Bot
from claude_telegram.config import Config
from claude_telegram.log import JsonlLogger
from claude_telegram.push import build_app
from claude_telegram.ratelimit import SlidingWindowLimiter
from claude_telegram.runner import ClaudeRunner
from claude_telegram.state import StateStore


async def _main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    cfg = Config.from_env()
    state_dir = Path(cfg.state_dir)
    state_dir.mkdir(parents=True, exist_ok=True)
    state = StateStore(state_dir / "state.json")
    logger = JsonlLogger(state_dir / "log.jsonl")
    limiter = SlidingWindowLimiter(max_events=cfg.rate_limit_per_hour, window_seconds=3600)
    runner = ClaudeRunner()
    bot = Bot(config=cfg, state=state, runner=runner, logger=logger, limiter=limiter)

    async def push_send(chat_id: str, text: str) -> int:
        return await bot.send(chat_id, text)

    push_app = build_app(
        push_token=cfg.push_token,
        default_chat_id=cfg.default_chat_id,
        send=push_send,
    )

    # Start Telegram polling and push server concurrently.
    await bot.app.initialize()
    await bot.app.start()
    polling = asyncio.create_task(bot.app.updater.start_polling())

    runner_app = web.AppRunner(push_app)
    await runner_app.setup()
    site = web.TCPSite(runner_app, cfg.push_host, cfg.push_port)
    await site.start()
    logging.info("listening on %s:%d", cfg.push_host, cfg.push_port)

    stop = asyncio.Event()
    try:
        await stop.wait()
    finally:
        polling.cancel()
        await bot.app.updater.stop()
        await bot.app.stop()
        await bot.app.shutdown()
        await runner_app.cleanup()


def main() -> None:
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Smoke-test imports**

Run: `.venv/bin/python -c "from claude_telegram import __main__"`
Expected: no output, exits 0.

- [ ] **Step 3: Commit**

```bash
git add -A && git commit -m "feat: __main__ entrypoint wiring bot + push server"
```

---

### Task 11: tg-push CLI helper

**Files:**
- Create: `scripts/tg-push`
- Test: `tests/test_tgpush_cli.py`

- [ ] **Step 1: Write the failing test**

`tests/test_tgpush_cli.py`:
```python
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch
import http.server
import socketserver
import threading
import json


def _start_capture_server(port: int) -> tuple[socketserver.TCPServer, list[dict]]:
    captured: list[dict] = []

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers["Content-Length"])
            body = self.rfile.read(length)
            captured.append({
                "path": self.path,
                "auth": self.headers.get("Authorization"),
                "body": json.loads(body),
            })
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok":true,"message_ids":[1]}')

        def log_message(self, *a, **k):
            pass

    srv = socketserver.TCPServer(("127.0.0.1", port), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, captured


def test_cli_posts_stdin_to_push(tmp_path: Path):
    srv, captured = _start_capture_server(0)
    port = srv.server_address[1]
    env_file = tmp_path / "env"
    env_file.write_text(f"PUSH_TOKEN=secret\nPUSH_URL=http://127.0.0.1:{port}/push\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "tg-push"
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            input="hello from cli",
            text=True,
            capture_output=True,
            env={**os.environ, "TG_PUSH_ENV_FILE": str(env_file)},
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert len(captured) == 1
        assert captured[0]["auth"] == "Bearer secret"
        assert captured[0]["body"]["text"] == "hello from cli"
    finally:
        srv.shutdown()
```

- [ ] **Step 2: Run test to verify failure**

Run: `.venv/bin/pytest tests/test_tgpush_cli.py -v`
Expected: FileNotFoundError on `scripts/tg-push`.

- [ ] **Step 3: Implement `scripts/tg-push`**

```python
#!/usr/bin/env python3
"""Tiny CLI that POSTs text to the claude-telegram push endpoint.

Reads PUSH_TOKEN and PUSH_URL from an env file (default ~/.claude-telegram/env,
override with $TG_PUSH_ENV_FILE). PUSH_URL defaults to http://127.0.0.1:8787/push.

Usage:
  echo "msg" | tg-push
  tg-push --title "Pionex daily" --file /tmp/brief.md
  tg-push --chat-id 9999 "inline text"
"""
import argparse
import os
import sys
import urllib.request
import json
from pathlib import Path


def load_env_file(path: Path) -> dict:
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("text", nargs="?", help="inline text; otherwise read stdin or --file")
    parser.add_argument("--title", help="optional title prepended as a bolded line")
    parser.add_argument("--file", help="read text from file")
    parser.add_argument("--chat-id", help="override default chat id")
    args = parser.parse_args()

    env_path = Path(os.environ.get("TG_PUSH_ENV_FILE", os.path.expanduser("~/.claude-telegram/env")))
    env = load_env_file(env_path)
    token = env.get("PUSH_TOKEN") or os.environ.get("PUSH_TOKEN")
    url = env.get("PUSH_URL") or os.environ.get("PUSH_URL", "http://127.0.0.1:8787/push")
    if not token:
        print("error: PUSH_TOKEN not set", file=sys.stderr)
        return 2

    if args.file:
        body = Path(args.file).read_text()
    elif args.text:
        body = args.text
    else:
        body = sys.stdin.read()
    if args.title:
        body = f"*{args.title}*\n\n{body}"

    payload = {"text": body}
    if args.chat_id:
        payload["chat_id"] = args.chat_id

    req = urllib.request.Request(
        url,
        method="POST",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            print(resp.read().decode())
        return 0
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Make executable and verify pass**

```bash
chmod +x scripts/tg-push
.venv/bin/pytest tests/test_tgpush_cli.py -v
```
Expected: 1 passed.

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat: tg-push CLI helper for cron jobs"
```

---

### Task 12: End-to-end integration test

**Files:**
- Test: `tests/test_integration.py`

Wire StreamRenderer + ClaudeRunner with a fake `claude` script and a fake sink. Asserts the renderer drives the sink with the events from the runner.

- [ ] **Step 1: Write the failing test**

`tests/test_integration.py`:
```python
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

    async def edit(self, message_id: int, text: str) -> None:
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
    assert "🔧 Bash" in body
    assert "echo hi" in body
    assert " done." in body
    assert "✅" in body
    assert "$0.0001" in body
```

- [ ] **Step 2: Run and verify pass**

Run: `.venv/bin/pytest tests/test_integration.py -v`
Expected: 1 passed.

- [ ] **Step 3: Run the full suite**

Run: `.venv/bin/pytest -v`
Expected: all tests pass (≈20+).

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "test: end-to-end runner→renderer integration"
```

---

### Task 13: systemd unit + install script

**Files:**
- Create: `systemd/claude-telegram.service`, `scripts/install.sh`

- [ ] **Step 1: Create systemd unit**

`systemd/claude-telegram.service`:
```ini
[Unit]
Description=Claude Telegram bridge
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
EnvironmentFile=%h/.claude-telegram/env
WorkingDirectory=%h/claude-telegram
ExecStart=%h/claude-telegram/.venv/bin/python -m claude_telegram
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

- [ ] **Step 2: Create `scripts/install.sh`**

```bash
#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$HOME/.claude-telegram"
ENV_FILE="$STATE_DIR/env"
UNIT_SRC="$ROOT/systemd/claude-telegram.service"
UNIT_DST="$HOME/.config/systemd/user/claude-telegram.service"
BIN_DIR="$HOME/.local/bin"

mkdir -p "$STATE_DIR" "$(dirname "$UNIT_DST")" "$BIN_DIR"
chmod 700 "$STATE_DIR"

if [ ! -d "$ROOT/.venv" ]; then
    echo "creating venv…"
    python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/pip" install --quiet -r "$ROOT/requirements.txt"

if [ ! -f "$ENV_FILE" ]; then
    echo "Setting up $ENV_FILE"
    read -r -p "Telegram bot token (from @BotFather): " TOKEN
    read -r -p "Your Telegram user ID (from @userinfobot): " USER_ID
    PUSH_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
    umask 077
    cat > "$ENV_FILE" <<EOF
TELEGRAM_BOT_TOKEN=$TOKEN
ALLOWED_USER_ID=$USER_ID
DEFAULT_CHAT_ID=$USER_ID
PUSH_TOKEN=$PUSH_TOKEN
PUSH_URL=http://127.0.0.1:8787/push
EOF
    chmod 600 "$ENV_FILE"
    echo "wrote $ENV_FILE (chmod 600)"
else
    echo "$ENV_FILE already exists; leaving it alone"
fi

install -m 0755 "$ROOT/scripts/tg-push" "$BIN_DIR/tg-push"
echo "installed $BIN_DIR/tg-push"

install -m 0644 "$UNIT_SRC" "$UNIT_DST"
echo "installed $UNIT_DST"

loginctl enable-linger "$USER" >/dev/null 2>&1 || true
systemctl --user daemon-reload
systemctl --user enable --now claude-telegram.service
systemctl --user status claude-telegram.service --no-pager
```

- [ ] **Step 3: Make executable**

```bash
chmod +x scripts/install.sh
```

- [ ] **Step 4: Commit**

```bash
git add -A && git commit -m "feat: systemd unit + install script"
```

---

### Task 14: README

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write README**

```markdown
# claude-telegram

Telegram bridge to the `claude` CLI. Talk to Claude Code from your phone with full skill/MCP parity. Bonus: a localhost push endpoint so cron jobs can DM you.

## Install (Aspire host)

```bash
git clone <repo> ~/claude-telegram
cd ~/claude-telegram
./scripts/install.sh
```

You'll be prompted for:
- **Telegram bot token** — create one via [@BotFather](https://t.me/BotFather).
- **Your Telegram user ID** — DM [@userinfobot](https://t.me/userinfobot) to get it.

The installer writes `~/.claude-telegram/env`, installs `tg-push` to `~/.local/bin/`, enables a user systemd service, and starts it.

## Commands

- `/reset` — clear the conversation, start fresh
- `/cd <path>` — change Claude's working directory for this chat
- `/cwd` — show current working directory
- `/cost` — show cost of the last turn
- `/stop` — interrupt the in-flight turn

## Pushing from cron

```bash
echo "BTC +2.1%" | tg-push
tg-push --title "Morning brief" --file /tmp/brief.md
```

## Files

- `~/.claude-telegram/env` — config (chmod 600)
- `~/.claude-telegram/state.json` — per-chat session IDs and cwd
- `~/.claude-telegram/log.jsonl` — append-only event log

## Logs

```bash
journalctl --user -u claude-telegram -f
tail -f ~/.claude-telegram/log.jsonl
```

## Safety

- Only the configured `ALLOWED_USER_ID` can talk to the bot. Other users are dropped silently.
- The push endpoint binds to `127.0.0.1` only and requires a bearer token.
- `claude` runs with `--dangerously-skip-permissions`. Same blast radius as you running Claude Code interactively. If your phone is lost, revoke the bot token via @BotFather.
```

- [ ] **Step 2: Commit**

```bash
git add README.md && git commit -m "docs: README"
```

---

## Self-review notes

- Spec coverage: all spec sections (architecture, components, sessions, commands, push, config, safety, logging, deployment, testing) have at least one task.
- No placeholders ("TBD", "implement later") in any task.
- Type consistency: `chat_id` is a string everywhere (`StateStore`, push payload, sink, bot). `session_id` is `Optional[str]`. `RunnerEvent.data` is `dict`. `MessageSink` protocol is the only interface between StreamRenderer and Telegram.
- The `Bot` class is glue: its protected methods are exercised end-to-end in task 12 via the runner→renderer path. Pure helpers (`is_authorized`, `parse_command`) get unit tests. Live Telegram smoke-testing happens after `install.sh` runs on Aspire.
