import asyncio
import json
from dataclasses import dataclass
from typing import AsyncIterator, Optional


@dataclass
class RunnerEvent:
    kind: str  # "session" | "text" | "tool_use" | "tool_result" | "result" | "error"
    data: dict


class ClaudeRunner:
    """
    Spawns the claude CLI and yields normalized events from its stream-json output.

    One instance owns at most one subprocess, so `terminate()` can only ever kill
    the turn this instance started. Create a fresh runner per turn — see
    `Bot._run_turn` — rather than sharing one across concurrent chats.
    """

    def __init__(self, claude_cmd: list[str] | None = None):
        # Allow override for tests; default is the real CLI.
        self.claude_cmd = claude_cmd or ["claude"]
        self._proc: asyncio.subprocess.Process | None = None

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
        except BaseException:
            # The consumer broke out, raised, or was cancelled (GeneratorExit).
            # Reap the child so we don't orphan a claude process — but never
            # yield during cleanup: a yield here would mask the real exception
            # with "async generator ignored GeneratorExit".
            if proc.returncode is None:
                proc.kill()
            await proc.wait()
            raise

        # Normal completion: stdout hit EOF, so it is safe to yield again.
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
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
