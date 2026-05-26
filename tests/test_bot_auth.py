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
