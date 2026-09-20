"""The cwd carried by the TUI's session.create must beat a shared gateway's defaults."""

from hermes_state import SessionDB
from tui_gateway import server


def test_tui_create_cwd_controls_session_and_tool_directory(monkeypatch, tmp_path):
    chosen = tmp_path / "chosen workspace"
    configured = tmp_path / "gateway default"
    chosen.mkdir()
    configured.mkdir()
    monkeypatch.setattr(server, "_launch_configured_cwd", lambda: str(configured))
    monkeypatch.setenv("TERMINAL_CWD", str(configured))
    monkeypatch.setattr(server, "_schedule_agent_build", lambda sid: None)
    monkeypatch.setattr(server, "_schedule_session_cap_enforcement", lambda: None)
    db = SessionDB(db_path=tmp_path / "state.db")
    monkeypatch.setattr(server, "_get_db", lambda: db)
    sid = None
    try:
        response = server.handle_request({
            "id": "create", "method": "session.create",
            "params": {"cols": 80, "cwd": str(chosen)},
        })
        assert "result" in response, response
        result = response["result"]
        sid = result["session_id"]
        session = server._sessions[sid]
        assert result["info"]["cwd"] == str(chosen)
        assert session["cwd"] == str(chosen)
        assert session["explicit_cwd"] is True
        assert server._terminal_task_cwd(session) == str(chosen)
    finally:
        if sid:
            server._sessions.pop(sid, None)
        db.close()
