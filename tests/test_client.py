"""Tests for cli/soft_ue_cli/client.py — uses httpx mock transport."""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import pytest

from soft_ue_cli import client as client_mod
from soft_ue_cli.client import call_tool, health_check
from soft_ue_cli.errors import BridgeError

_DUMMY_REQUEST = httpx.Request("POST", "http://127.0.0.1:8080/bridge")

def _resp(status: int, body: dict | None = None, text: str | None = None) -> httpx.Response:
    """Build an httpx.Response with a dummy request so raise_for_status() works."""
    if text is not None:
        r = httpx.Response(status, text=text, request=_DUMMY_REQUEST)
    else:
        r = httpx.Response(status, json=body or {}, request=_DUMMY_REQUEST)
    return r

def _patch_url(url: str = "http://127.0.0.1:8080"):
    return patch("soft_ue_cli.client.get_server_url", return_value=url)

@pytest.fixture(autouse=True)
def _isolated_session_identity(monkeypatch):
    """No test inherits another's session identity.

    Both halves are load-bearing: the thread label survives between tests on the
    pytest main thread, and create_server() (test_mcp_server.py) sets the
    process-wide default for the rest of the run.
    """
    client_mod.clear_session_label()
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    yield
    client_mod.clear_session_label()

# -- call_tool -----------------------------------------------------------------

def test_call_tool_success(monkeypatch):
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": '{"actors": []}'}]},
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        result = call_tool("query-level", {})
    assert result == {"actors": []}

def test_call_tool_text_content_non_json(monkeypatch):
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": "plain text response"}]},
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        result = call_tool("get-logs", {})
    assert result == {"text": "plain text response"}

def test_call_tool_result_is_error(monkeypatch):
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"isError": True, "content": [{"text": "actor not found"}]},
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("spawn-actor", {"class": "BadClass"})
    assert "actor not found" in str(exc.value)

def test_call_tool_jsonrpc_error(monkeypatch):
    payload = {"jsonrpc": "2.0", "id": "1", "error": {"code": -32601, "message": "Method not found"}}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("unknown-tool", {})
    assert "Method not found" in str(exc.value)

def test_call_tool_connect_error(monkeypatch):
    def raise_connect(*args, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", raise_connect)
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("status", {})
    assert "cannot connect to SoftUEBridge" in str(exc.value)

def test_call_tool_falls_back_when_forced_port_is_stale(monkeypatch, capsys):
    calls: list[str] = []
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": '{"status": "ok"}'}]},
    }

    def post_with_stale_port(url, **kw):
        calls.append(url)
        if url == "http://127.0.0.1:8081/bridge":
            raise httpx.ConnectError("refused")
        assert url == "http://127.0.0.1:8080/bridge"
        return _resp(200, payload)

    monkeypatch.setattr(httpx, "post", post_with_stale_port)
    monkeypatch.setattr(client_mod, "get_forced_port_fallback_url", lambda current: "http://127.0.0.1:8080")
    monkeypatch.setattr(
        client_mod,
        "_handle_startup_recovery_for_connection",
        lambda: pytest.fail("startup recovery should not run when registry fallback is live"),
    )
    with _patch_url("http://127.0.0.1:8081"):
        result = call_tool("status", {})

    assert result == {"status": "ok"}
    assert calls == ["http://127.0.0.1:8081/bridge", "http://127.0.0.1:8080/bridge"]
    assert "SOFT_UE_BRIDGE_PORT appears stale" in capsys.readouterr().err

def test_call_tool_falls_back_when_forced_port_hits_non_bridge_service(monkeypatch, capsys):
    calls: list[str] = []
    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": '{"status": "ok"}'}]},
    }

    def post_with_non_bridge(url, **kw):
        calls.append(url)
        if url == "http://127.0.0.1:8081/bridge":
            response = httpx.Response(404, request=httpx.Request("POST", url))
            raise httpx.HTTPStatusError("404", request=response.request, response=response)
        assert url == "http://127.0.0.1:8080/bridge"
        return _resp(200, payload)

    monkeypatch.setattr(httpx, "post", post_with_non_bridge)
    monkeypatch.setattr(client_mod, "get_forced_port_fallback_url", lambda current: "http://127.0.0.1:8080")
    with _patch_url("http://127.0.0.1:8081"):
        result = call_tool("status", {})

    assert result == {"status": "ok"}
    assert calls == ["http://127.0.0.1:8081/bridge", "http://127.0.0.1:8080/bridge"]
    assert "SOFT_UE_BRIDGE_PORT appears stale" in capsys.readouterr().err

def test_call_tool_handles_remembered_startup_recovery_and_retries(monkeypatch):
    calls = 0

    def maybe_connect(*args, **kw):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("refused")
        return _resp(200, {"jsonrpc": "2.0", "id": "1", "result": {"content": [{"type": "text", "text": "{}"}]}})

    handled: list[str | None] = []
    monkeypatch.setattr(httpx, "post", maybe_connect)
    monkeypatch.setattr(
        client_mod,
        "_handle_startup_recovery_for_connection",
        lambda: handled.append("handled") or "handled startup recovery prompt",
    )
    with _patch_url():
        result = call_tool("status", {})

    assert result == {}
    assert calls == 2
    assert handled == ["handled"]

def test_connection_recovery_prompts_interactive_user(monkeypatch):
    import soft_ue_cli.startup_recovery as startup_recovery

    calls: list[tuple[str, bool | None]] = []

    def fake_recovery(action, *, interactive=None):
        calls.append((action, interactive))
        return SimpleNamespace(action="recover")

    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(startup_recovery, "handle_startup_recovery_prompt", fake_recovery)

    note = client_mod._handle_startup_recovery_for_connection()

    assert note == "handled Unreal startup recovery prompt with action 'recover'"
    assert calls == [("ask", True)]

def test_call_tool_http_error(monkeypatch):
    def raise_http(*args, **kw):
        response = httpx.Response(500)
        raise httpx.HTTPStatusError("500", request=httpx.Request("POST", "http://x"), response=response)

    monkeypatch.setattr(httpx, "post", raise_http)
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("status", {})
    assert "HTTP 500" in str(exc.value)

def test_call_tool_non_json_response(monkeypatch):
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, text="<html>not json</html>"))
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("status", {})
    assert "server returned non-JSON response" in str(exc.value)

def test_call_tool_empty_result(monkeypatch):
    payload = {"jsonrpc": "2.0", "id": "1", "result": {}}
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        result = call_tool("set-console-var", {"name": "r.VSync", "value": "0"})
    assert result == {}

# -- health_check --------------------------------------------------------------

def test_health_check_success(monkeypatch):
    req = httpx.Request("GET", "http://127.0.0.1:8080/bridge")
    monkeypatch.setattr(httpx, "get", lambda url, **kw: httpx.Response(200, json={"status": "ok"}, request=req))
    with _patch_url():
        result = health_check()
    assert result == {"status": "ok"}

def test_health_check_connection_error(monkeypatch):
    def raise_exc(*args, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", raise_exc)
    with _patch_url():
        result = health_check()
    assert "error" in result

def test_health_check_falls_back_when_forced_port_is_stale(monkeypatch):
    req = httpx.Request("GET", "http://127.0.0.1:8080/bridge")
    calls: list[str] = []

    def get_with_stale_port(url, **kw):
        calls.append(url)
        if url == "http://127.0.0.1:8081/bridge":
            raise httpx.ConnectError("refused")
        assert url == "http://127.0.0.1:8080/bridge"
        return httpx.Response(200, json={"status": "ok"}, request=req)

    monkeypatch.setattr(httpx, "get", get_with_stale_port)
    monkeypatch.setattr(client_mod, "get_forced_port_fallback_url", lambda current: "http://127.0.0.1:8080")
    with _patch_url("http://127.0.0.1:8081"):
        result = health_check()

    assert result["status"] == "ok"
    assert "SOFT_UE_BRIDGE_PORT appears stale" in result["warning"]
    assert result["bridge_url"] == "http://127.0.0.1:8080"
    assert calls == ["http://127.0.0.1:8081/bridge", "http://127.0.0.1:8080/bridge"]

def test_health_check_falls_back_when_forced_port_hits_non_bridge_service(monkeypatch):
    calls: list[str] = []

    def get_with_non_bridge(url, **kw):
        calls.append(url)
        if url == "http://127.0.0.1:8081/bridge":
            response = httpx.Response(404, request=httpx.Request("GET", url))
            raise httpx.HTTPStatusError("404", request=response.request, response=response)
        return httpx.Response(200, json={"status": "ok"}, request=httpx.Request("GET", url))

    monkeypatch.setattr(httpx, "get", get_with_non_bridge)
    monkeypatch.setattr(client_mod, "get_forced_port_fallback_url", lambda current: "http://127.0.0.1:8080")
    with _patch_url("http://127.0.0.1:8081"):
        result = health_check()

    assert result["status"] == "ok"
    assert "SOFT_UE_BRIDGE_PORT appears stale" in result["warning"]
    assert calls == ["http://127.0.0.1:8081/bridge", "http://127.0.0.1:8080/bridge"]

def test_health_check_timeout(monkeypatch):
    def raise_timeout(*args, **kw):
        raise httpx.TimeoutException("timeout")

    monkeypatch.setattr(httpx, "get", raise_timeout)
    with _patch_url():
        result = health_check()
    assert "error" in result

# -- session identity ------------------------------------------------------------

def test_session_descriptor_is_derived_when_nothing_declared(monkeypatch):
    from soft_ue_cli import client as client_mod

    monkeypatch.setattr(client_mod, "_CLIENT_KIND", "cli")
    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)

    desc = client_mod.session_descriptor()

    assert desc["id"].startswith("unknown:")
    assert desc["label"] == ""
    assert desc["confidence"] == "derived"
    assert desc["client"] == "cli"
    assert len(desc["origin"]) == 8

def test_session_descriptor_prefers_explicit_label_over_env(monkeypatch):
    from soft_ue_cli import client as client_mod

    monkeypatch.setenv("SOFT_UE_SESSION", "from-env")
    client_mod.set_session_label("cape-cloth")

    desc = client_mod.session_descriptor()

    assert desc["id"] == "cape-cloth"
    assert desc["label"] == "cape-cloth"
    assert desc["confidence"] == "declared"

def test_session_descriptor_falls_back_to_env(monkeypatch):
    from soft_ue_cli import client as client_mod

    monkeypatch.setenv("SOFT_UE_SESSION", "from-env")

    assert client_mod.session_descriptor()["id"] == "from-env"

def test_session_label_does_not_leak_between_threads(monkeypatch):
    """The MCP server is one process on pooled worker threads.

    A label declared by one tool call must not become the identity of every
    later bridge call from that server.
    """
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    seen: dict[str, str] = {}

    def worker():
        client_mod.set_session_label("cape-cloth")
        seen["worker"] = client_mod.session_descriptor()["id"]

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert seen["worker"] == "cape-cloth"
    assert client_mod.session_descriptor()["id"].startswith("unknown:")

def test_clear_session_label_releases_a_pooled_thread(monkeypatch):
    """Worker threads are reused, so the next call must not inherit the label."""
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    client_mod.set_session_label("cape-cloth")
    client_mod.clear_session_label()

    assert client_mod.session_descriptor()["confidence"] == "derived"

def test_mcp_startup_default_applies_without_a_per_call_label(monkeypatch):
    """create_server()'s m-<uuid8> is a legitimate process-wide identity."""
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    client_mod.set_default_session_label("m-ab12cd34")

    desc = client_mod.session_descriptor()

    assert desc["id"] == "m-ab12cd34"
    assert desc["confidence"] == "declared"

def test_thread_label_wins_over_the_process_default(monkeypatch):
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    client_mod.set_default_session_label("m-ab12cd34")
    client_mod.set_session_label("cape-cloth")

    assert client_mod.session_descriptor()["id"] == "cape-cloth"

def test_origin_id_is_stable_per_directory(monkeypatch, tmp_path):
    from soft_ue_cli import client as client_mod

    monkeypatch.chdir(tmp_path)
    first = client_mod.session_descriptor()["origin"]
    second = client_mod.session_descriptor()["origin"]

    assert first == second

# -- notice extraction ------------------------------------------------------------

def test_call_tool_ex_returns_notices_from_result_sibling(monkeypatch):
    from soft_ue_cli.client import call_tool_ex

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "content": [{"type": "text", "text": '{"actors": []}'}],
            "session_notices": [{"seq": 4, "kind": "ask", "text": "can I rebuild?"}],
        },
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        result, meta = call_tool_ex("query-level", {})

    assert result == {"actors": []}
    assert meta.notices == [{"seq": 4, "kind": "ask", "text": "can I rebuild?"}]

def test_call_tool_ex_returns_empty_notices_when_absent(monkeypatch):
    from soft_ue_cli.client import call_tool_ex

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {"content": [{"type": "text", "text": '{"actors": []}'}]},
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        _, meta = call_tool_ex("query-level", {})

    assert meta.notices == []

def test_call_tool_sends_session_descriptor_in_params(monkeypatch):
    from soft_ue_cli import client as client_mod

    captured = {}

    def fake_post(url, **kw):
        captured["json"] = kw["json"]
        return _resp(200, {
            "jsonrpc": "2.0",
            "id": "1",
            "result": {"content": [{"type": "text", "text": "{}"}]},
        })

    monkeypatch.setattr(httpx, "post", fake_post)
    client_mod.set_session_label("cape-cloth")
    with _patch_url():
        client_mod.call_tool("query-level", {})

    assert captured["json"]["params"]["_session"]["id"] == "cape-cloth"
    assert "_session" not in captured["json"]["params"]["arguments"]

def test_bridge_error_carries_notices(monkeypatch):
    from soft_ue_cli.client import call_tool
    from soft_ue_cli.errors import BridgeError

    payload = {
        "jsonrpc": "2.0",
        "id": "1",
        "result": {
            "content": [{"type": "text", "text": "boom"}],
            "isError": True,
            "session_notices": [{"seq": 9, "kind": "notice", "text": "builder started a rebuild"}],
        },
    }
    monkeypatch.setattr(httpx, "post", lambda url, **kw: _resp(200, payload))
    with _patch_url():
        with pytest.raises(BridgeError) as excinfo:
            call_tool("query-level", {})

    assert excinfo.value.notices == [
        {"seq": 9, "kind": "notice", "text": "builder started a rebuild"}
    ]

# -- session_postmortem ------------------------------------------------------------

def _iso_utc(age_minutes: float = 0.0) -> str:
    """A timestamp in FDateTime::ToIso8601 shape, aged by age_minutes."""
    stamp = datetime.now(timezone.utc) - timedelta(minutes=age_minutes)
    return f"{stamp.strftime('%Y-%m-%dT%H:%M:%S')}.{stamp.microsecond // 1000:03d}Z"

def test_session_postmortem_names_who_shut_the_editor_down(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "written_utc": _iso_utc(1),
        "sessions": [
            {"id": "codex:cloth-weld", "label": "codex:cloth-weld", "state": "active"}
        ],
        "shutdown_intent": {
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch; the editor is shutting down for a rebuild",
            "created_utc": "2026-07-26T05:09:14Z",
        },
    }))

    text = session_postmortem(tmp_path)

    assert "codex:cloth-weld" in text
    assert "build-and-relaunch" in text
    assert "05:09:14" in text

def test_session_postmortem_suppresses_a_stale_shutdown_intent(tmp_path):
    """A shutdown_intent outlives the editor that wrote it.

    Rehydrate restores only the sessions array, and Touch/ClaimResource never
    flush, so a days-old intent can still be on disk when a *different* editor
    is taskkilled or crashes. Naming that session would be confidently wrong.
    """
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "written_utc": _iso_utc(60 * 48),
        "sessions": [
            {"id": "codex:cloth-weld", "label": "codex:cloth-weld", "state": "active"}
        ],
        "shutdown_intent": {
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch; the editor is shutting down for a rebuild",
            "created_utc": "2026-07-24T05:09:14Z",
        },
    }))

    text = session_postmortem(tmp_path)

    assert "The editor is gone" not in text
    assert "build-and-relaunch" not in text
    assert text == "  Last known sessions in this project: codex:cloth-weld"

def test_session_postmortem_suppresses_a_shutdown_intent_without_written_utc(tmp_path):
    """Every registry that can write shutdown_intent also writes written_utc.

    Both keys landed in 50d4c68, so a file missing the timestamp is foreign or
    truncated — old enough to distrust, not old enough to attribute.
    """
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "sessions": [{"id": "codex:cloth-weld", "label": "codex:cloth-weld"}],
        "shutdown_intent": {
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch",
            "created_utc": "2026-07-26T05:09:14Z",
        },
    }))

    text = session_postmortem(tmp_path)

    assert "build-and-relaunch" not in text
    assert text == "  Last known sessions in this project: codex:cloth-weld"

@pytest.mark.parametrize("written_utc", [12345, "nope", None, {}, "", "2026-13-45T99:99Z"])
def test_session_postmortem_survives_a_bad_written_utc(tmp_path, written_utc):
    """The freshness check must degrade, not throw, and not eat the fallback.

    session_postmortem() wraps everything in one `except Exception: return ""`,
    so a TypeError in the age computation would silently delete the rest of the
    post-mortem too. Asserting the fallback line still lands catches that;
    asserting "it did not raise" would not.
    """
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "written_utc": written_utc,
        "sessions": [{"id": "codex:cloth-weld", "label": "codex:cloth-weld"}],
        "shutdown_intent": {
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch",
            "created_utc": "2026-07-26T05:09:14Z",
        },
    }))

    assert session_postmortem(tmp_path) == (
        "  Last known sessions in this project: codex:cloth-weld"
    )

def test_session_postmortem_accepts_a_naive_written_utc(tmp_path):
    """ToIso8601 has carried a trailing Z, but a naive stamp must read as UTC."""
    from soft_ue_cli.client import session_postmortem

    naive = datetime.now(timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%S")
    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "written_utc": naive,
        "sessions": [{"id": "codex:cloth-weld", "label": "codex:cloth-weld"}],
        "shutdown_intent": {
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch",
            "created_utc": "2026-07-26T05:09:14Z",
        },
    }))

    assert "build-and-relaunch" in session_postmortem(tmp_path)

def test_session_postmortem_is_empty_without_records(tmp_path):
    from soft_ue_cli.client import session_postmortem

    assert session_postmortem(tmp_path) == ""

def test_session_postmortem_survives_malformed_json(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text("{not json")

    assert session_postmortem(tmp_path) == ""

def test_session_postmortem_survives_root_not_an_object(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps(["just", "a", "list"]))

    assert session_postmortem(tmp_path) == ""

def test_session_postmortem_survives_shutdown_intent_not_an_object(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "shutdown_intent": "oops a string",
        "sessions": [],
    }))

    assert session_postmortem(tmp_path) == ""

def test_session_postmortem_survives_non_dict_session_entries(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "sessions": ["not", "a", "dict"],
    }))

    assert session_postmortem(tmp_path) == ""

def test_session_postmortem_names_last_known_sessions_without_shutdown_intent(tmp_path):
    from soft_ue_cli.client import session_postmortem

    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "sessions": [
            {"id": "codex:cloth-weld", "label": "codex:cloth-weld", "state": "idle"},
            {"id": "claude:parkour", "label": "claude:parkour", "state": "idle"},
        ],
    }))

    text = session_postmortem(tmp_path)

    assert text == "  Last known sessions in this project: codex:cloth-weld, claude:parkour"

def test_connect_error_message_includes_postmortem(monkeypatch, tmp_path):
    """The motivating path: no mocked session_postmortem, a real file, a real cwd walk-up."""
    bridge_dir = tmp_path / ".soft-ue-bridge"
    bridge_dir.mkdir()
    (bridge_dir / "sessions.json").write_text(json.dumps({
        "written_utc": _iso_utc(1),
        "sessions": [
            {"id": "codex:cloth-weld", "label": "codex:cloth-weld", "state": "active"}
        ],
        "shutdown_intent": {
            "from": "codex:cloth-weld",
            "from_label": "codex:cloth-weld",
            "text": "ran build-and-relaunch; the editor is shutting down for a rebuild",
            "created_utc": "2026-07-26T05:09:14Z",
        },
    }))
    monkeypatch.chdir(tmp_path)

    def raise_connect(*args, **kw):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "post", raise_connect)
    monkeypatch.setattr(client_mod, "_handle_startup_recovery_for_connection", lambda: None)
    with _patch_url():
        with pytest.raises(BridgeError) as exc:
            call_tool("status", {})

    assert "cannot connect to SoftUEBridge" in str(exc.value)
    assert "codex:cloth-weld" in str(exc.value)
    assert "The editor is gone" in str(exc.value)
