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
