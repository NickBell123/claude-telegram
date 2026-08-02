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


def _run_cli(tmp_path: Path, args: list[str], stdin: str) -> dict:
    """Run tg-push against a capture server and return the POSTed body."""
    srv, captured = _start_capture_server(0)
    port = srv.server_address[1]
    env_file = tmp_path / "env"
    env_file.write_text(f"PUSH_TOKEN=secret\nPUSH_URL=http://127.0.0.1:{port}/push\n")
    script = Path(__file__).resolve().parents[1] / "scripts" / "tg-push"
    try:
        result = subprocess.run(
            [sys.executable, str(script), *args],
            input=stdin,
            text=True,
            capture_output=True,
            env={**os.environ, "TG_PUSH_ENV_FILE": str(env_file)},
            timeout=5,
        )
        assert result.returncode == 0, result.stderr
        assert len(captured) == 1
        return captured[0]["body"]
    finally:
        srv.shutdown()


def test_title_is_sent_as_real_bold_not_literal_asterisks(tmp_path: Path):
    body = _run_cli(tmp_path, ["--title", "Morning brief"], "BTC up")
    assert body["parse_mode"] == "HTML"
    assert body["text"].startswith("<b>Morning brief</b>")
    assert "*" not in body["text"]


def test_title_mode_escapes_markup_in_the_body(tmp_path: Path):
    # A generated brief containing < or & must not be read as markup.
    body = _run_cli(tmp_path, ["--title", "T"], "profit <10% & rising")
    assert "profit &lt;10% &amp; rising" in body["text"]


def test_explicit_parse_mode_is_passed_through_unescaped(tmp_path: Path):
    body = _run_cli(tmp_path, ["--parse-mode", "MarkdownV2"], "*already formatted*")
    assert body["parse_mode"] == "MarkdownV2"
    assert body["text"] == "*already formatted*"


def test_plain_push_still_sends_no_parse_mode(tmp_path: Path):
    body = _run_cli(tmp_path, [], "just text")
    assert "parse_mode" not in body
    assert body["text"] == "just text"
