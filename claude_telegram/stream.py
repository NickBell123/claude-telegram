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
