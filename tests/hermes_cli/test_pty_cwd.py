"""Explicit PTY working directories must reach the real child, never silently fall back."""

import json
import os
import sys
from urllib.parse import urlencode

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from hermes_cli import web_server_chat as chat
from hermes_cli.web_routers import chat_ws


@pytest.fixture
def client(monkeypatch):
    async def gate(ws, kind):
        return "test", "test", "test"

    monkeypatch.setattr(chat_ws, "_ws_gate", gate)
    app = FastAPI()
    app.include_router(chat_ws.router)
    return TestClient(app)


@pytest.mark.linux_only
def test_explicit_cwd_reaches_real_pty_child(client, tmp_path, monkeypatch):
    target = tmp_path / "directory with spaces"
    target.mkdir()
    launch = tmp_path / "launch"
    launch.mkdir()

    async def argv(**kwargs):
        return [sys.executable, "-c", (
            "import os,json; print('CWD_RESULT='+json.dumps("
            "[os.getcwd(),os.getenv('HERMES_CWD'),os.getenv('TERMINAL_CWD'),"
            "os.getenv('HERMES_TUI_LAUNCH_CWD')]))"
        )], str(launch), dict(os.environ)

    monkeypatch.setattr(chat, "_resolve_chat_argv_async", argv)
    with client.websocket_connect("/api/pty?" + urlencode({"cwd": str(target)})) as ws:
        output = b""
        while b"CWD_RESULT=" not in output or b"\n" not in output.split(b"CWD_RESULT=", 1)[1]:
            frame = ws.receive()
            assert frame["type"] == "websocket.send", frame
            output += frame.get("bytes") or frame.get("text", "").encode()
    result = json.loads(output.split(b"CWD_RESULT=", 1)[1].splitlines()[0])
    assert result == [str(target)] * 4


@pytest.mark.parametrize("kind,reason", [
    ("empty", "cwd must not be empty"),
    ("missing", "cwd does not exist"),
    ("file", "cwd is not a directory"),
])
def test_invalid_cwd_is_rejected_before_resolution(client, tmp_path, monkeypatch, kind, reason):
    target = tmp_path / kind
    if kind == "file":
        target.write_text("not a directory")
    called = []

    async def argv(**kwargs):
        called.append(kwargs)
        return ["unused"], None, {}

    monkeypatch.setattr(chat, "_resolve_chat_argv_async", argv)
    monkeypatch.setattr(chat.PtyBridge, "spawn", lambda *a, **kw: None)

    async def pump(ws, bridge):
        await ws.send_text("incorrectly spawned")
        await ws.close()

    monkeypatch.setattr(chat_ws, "_legacy_pump", pump)
    query = {"cwd": "" if kind == "empty" else str(target)}
    with client.websocket_connect("/api/pty?" + urlencode(query)) as ws:
        assert reason in ws.receive_text()
        assert ws.receive()["code"] == 4400
    assert called == []


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("protocols", [[], ["hermes-pty-cwd-v1"], ["other"]])
def test_cwd_protocol_is_opt_in_and_omission_preserves_launch(
    client, tmp_path, monkeypatch, explicit, protocols,
):
    original_env = {"HERMES_CWD": "inherited", "TERMINAL_CWD": "configured"}
    spawned = []

    async def argv(**kwargs):
        assert kwargs == {"resume": None, "sidecar_url": None, "profile": None}
        return ["unused"], "legacy-launch-directory", original_env

    def spawn(argv, cwd=None, env=None):
        spawned.append((cwd, env))

    async def pump(ws, bridge):
        await ws.send_text("spawned")
        await ws.close()

    monkeypatch.setattr(chat, "_resolve_chat_argv_async", argv)
    monkeypatch.setattr(chat.PtyBridge, "spawn", spawn)
    monkeypatch.setattr(chat_ws, "_legacy_pump", pump)
    url = "/api/pty" + ("?" + urlencode({"cwd": str(tmp_path)}) if explicit else "")
    with client.websocket_connect(url, subprotocols=protocols) as ws:
        assert ws.accepted_subprotocol == (
            "hermes-pty-cwd-v1" if explicit and "hermes-pty-cwd-v1" in protocols else None
        )
        assert ws.receive_text() == "spawned"
    expected = (str(tmp_path), {
        "HERMES_CWD": str(tmp_path), "TERMINAL_CWD": str(tmp_path),
        "HERMES_TUI_LAUNCH_CWD": str(tmp_path),
    }) if explicit else (
        "legacy-launch-directory", original_env
    )
    assert spawned == [expected]
    assert original_env == {"HERMES_CWD": "inherited", "TERMINAL_CWD": "configured"}


def test_ordinary_tui_launch_drops_inherited_explicit_cwd_marker(tmp_path):
    from hermes_cli.main_tui_launch import _apply_tui_python_env

    env = {
        "HERMES_CWD": str(tmp_path), "HERMES_PYTHON": sys.executable,
        "HERMES_TUI_LAUNCH_CWD": "/previous-request",
    }
    _apply_tui_python_env(env)
    assert "HERMES_TUI_LAUNCH_CWD" not in env
    assert env["HERMES_CWD"] == str(tmp_path)


@pytest.mark.linux_only
@pytest.mark.parametrize("relative_python", [False, True])
def test_relative_prebuilt_tui_launch_survives_explicit_cwd(client, tmp_path, monkeypatch, relative_python):
    import shutil
    from hermes_cli import main_tui_launch

    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for the prebuilt TUI launch probe")
    monkeypatch.chdir(tmp_path)
    bundle = tmp_path / "bundle" / "dist"
    bundle.mkdir(parents=True)
    (bundle / "entry.js").write_text(
        "console.log('NODE_CWD=' + process.cwd());"
        "const child = require('node:child_process').spawnSync(process.env.HERMES_PYTHON,"
        "['-c', 'import os; print(os.getcwd())'], {encoding: 'utf8'});"
        "console.log('PYTHON_CWD=' + child.stdout);"
    )
    if relative_python:
        interpreter = tmp_path / "bin" / "python"
        interpreter.parent.mkdir()
        interpreter.symlink_to(sys.executable)
        monkeypatch.setenv("HERMES_PYTHON", "bin/python")
        monkeypatch.setenv("HERMES_CWD", str(tmp_path))
    target = tmp_path / "workspace"
    target.mkdir()
    monkeypatch.setenv("HERMES_TUI_DIR", "bundle")
    monkeypatch.setattr(main_tui_launch, "_ensure_tui_node", lambda: None)
    monkeypatch.setattr(main_tui_launch, "_tui_node_bin", lambda name: node)
    with client.websocket_connect("/api/pty?" + urlencode({"cwd": str(target)})) as ws:
        output = b""
        while True:
            frame = ws.receive()
            if frame["type"] == "websocket.close":
                break
            output += frame.get("bytes") or frame.get("text", "").encode()
    assert ("NODE_CWD=" + str(target)).encode() in output, output.decode()
    assert ("PYTHON_CWD=" + str(target)).encode() in output, output.decode()
