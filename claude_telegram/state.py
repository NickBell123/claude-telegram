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
    def __init__(self, path: Path, default_cwd: str | None = None):
        default_cwd = default_cwd or os.path.expanduser("~")
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
