"""Tests for cli/soft_ue_cli/mcp_server.py — MCP server tool/prompt registration."""

from __future__ import annotations

import json
import sys
from unittest.mock import patch

import pytest

# Skip all tests if mcp is not installed
mcp = pytest.importorskip("mcp")

from soft_ue_cli.client import BridgeCallMeta
from soft_ue_cli.errors import BridgeError, ErrorKind
from soft_ue_cli.mcp_schema import extract_tools
from soft_ue_cli.mcp_server import create_server, _make_client_tool_fn

def test_create_server_returns_fastmcp():
    from mcp.server.fastmcp import FastMCP
    server = create_server()
    assert isinstance(server, FastMCP)

def test_server_has_tools():
    # NOTE: _tool_manager/_tools are private FastMCP internals.
    # No public enumeration API exists as of mcp 1.26. Update if one is added.
    server = create_server()
    assert server._tool_manager is not None
    assert len(server._tool_manager._tools) >= 60

def test_server_has_prompts():
    server = create_server()
    assert server._prompt_manager is not None
    assert len(server._prompt_manager._prompts) > 0

def test_anim_state_machine_tools_have_native_mcp_types():
    tools = {tool["name"]: tool for tool in extract_tools()}

    assert tools["anim state-machine add"]["parameters"]["properties"]["position"]["type"] == "array"
    assert tools["anim state add"]["parameters"]["properties"]["position"]["type"] == "array"
    assert tools["anim transition add"]["parameters"]["properties"]["rule"]["type"] == "boolean"

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_mcp_cloth_chaos_set_weightmap_forwards_native_arrays(mock_call_tool):
    mock_call_tool.return_value = ({"success": True}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "cloth chaos-set-weightmap":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(
        cloth_asset="/Game/Cloth/CA_Cape",
        vertices=[0, 1, 2],
        center=[0.0, 0.0, 120.0],
        radius=25.0,
        value=0.0,
    )

    mock_call_tool.assert_called_once()
    call_args = mock_call_tool.call_args
    assert call_args.args[0] == "cloth-chaos-set-weightmap"
    assert call_args.args[1]["vertices"] == [0, 1, 2]
    assert call_args.args[1]["center"] == [0.0, 0.0, 120.0]
    assert call_args.args[1]["radius"] == 25.0
    assert json.loads(result) == {"success": True}

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_mcp_cloth_weld_forwards_native_center_array(mock_call_tool):
    mock_call_tool.return_value = ({"success": True}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "cloth weld":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(
        skeletal_mesh="/Game/Characters/SK_Cape",
        asset_name="CapeCloth",
        tolerance=0.25,
        center=[0.0, 0.0, 120.0],
        radius=30.0,
    )

    mock_call_tool.assert_called_once()
    call_args = mock_call_tool.call_args
    assert call_args.args[0] == "cloth-weld"
    assert call_args.args[1]["center"] == [0.0, 0.0, 120.0]
    assert call_args.args[1]["radius"] == 30.0
    assert json.loads(result) == {"success": True}

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_mcp_cloth_apply_weightmap_forwards_native_spatial_center_array(mock_call_tool):
    mock_call_tool.return_value = ({"success": True}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "cloth apply-weightmap":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(
        skeletal_mesh="/Game/Characters/SK_Cape",
        asset_name="CapeCloth",
        rule="spatial",
        center=[0.0, 0.0, 120.0],
        radius=30.0,
        min_value=0.0,
        max_value=20.0,
    )

    mock_call_tool.assert_called_once()
    call_args = mock_call_tool.call_args
    assert call_args.args[0] == "cloth-apply-weightmap"
    assert call_args.args[1]["center"] == [0.0, 0.0, 120.0]
    assert call_args.args[1]["radius"] == 30.0
    assert call_args.args[1]["min_value"] == 0.0
    assert call_args.args[1]["max_value"] == 20.0
    assert json.loads(result) == {"success": True}

@patch("soft_ue_cli.client.call_tool_ex")
def test_tool_call_forwards_to_bridge(mock_call_tool):
    mock_call_tool.return_value = ({"actors": []}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "query-level":
            tool_fn = tool.fn
            break

    assert tool_fn is not None, "query-level tool not registered"
    result = tool_fn(limit=10)
    mock_call_tool.assert_called_once()
    call_args = mock_call_tool.call_args
    assert call_args[0][0] == "query-level"
    assert "limit" in call_args[0][1]

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_tool_call_maps_no_auto_position(mock_call_tool):
    mock_call_tool.return_value = ({"status": "ok"}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "blueprint node add":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(asset_path="/Game/BP", node_class="K2Node_CallFunction", no_auto_position=True)
    mock_call_tool.assert_called_once()
    call_args = mock_call_tool.call_args
    assert call_args[0][0] == "add-graph-node"
    assert call_args.kwargs == {}
    arguments = call_args[0][1]
    assert arguments["auto_position"] is False
    assert "no_auto_position" not in arguments
    parsed = json.loads(result)
    assert parsed == {"status": "ok"}

@patch("soft_ue_cli.client.call_tool_ex")
def test_tool_call_forwards_pie_timeout_to_http_timeout(mock_call_tool):
    mock_call_tool.return_value = ({"status": "ok"}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "pie-session":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    tool_fn(action="start", timeout=42.5)
    mock_call_tool.assert_called_once()
    call_kwargs = mock_call_tool.call_args.kwargs
    assert call_kwargs["timeout"] == 42.5
    arguments = mock_call_tool.call_args.args[1]
    assert arguments["action"] == "start"
    assert arguments["timeout"] == 42.5

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_tool_call_normalizes_add_graph_node_created_nodes(mock_call_tool):
    mock_call_tool.return_value = (
        {
            "status": True,
            "created_nodes": [
                {"guid": "11111111-1111-1111-1111-111111111111", "class": "AnimGraphNode_Root"},
                {"guid": "22222222-2222-2222-2222-222222222222", "class": "AnimGraphNode_LinkedInputPose"},
            ],
        },
        BridgeCallMeta(),
    )
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "blueprint node add":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(asset_path="/Game/ALI", node_class="AnimLayerFunction", graph_name="ALIGraph")
    parsed = json.loads(result)
    assert parsed["node_guid"] == "11111111-1111-1111-1111-111111111111"

@patch("soft_ue_cli.__main__.call_tool_ex")
def test_mcp_add_co_parameter_uses_cli_transform(mock_call_tool):
    mock_call_tool.return_value = ({"success": True}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "mutable graph add-parameter":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(
        asset_path="/Game/Characters/CO_Hero.CO_Hero",
        name="BodyHeight",
        parameter_type="float",
    )

    mock_call_tool.assert_called_once()
    assert mock_call_tool.call_args.args[0] == "add-customizable-object-node"
    assert mock_call_tool.call_args.args[1] == {
        "asset_path": "/Game/Characters/CO_Hero.CO_Hero",
        "node_class": "CustomizableObjectNodeFloatParameter",
        "properties": {"ParameterName": "BodyHeight"},
    }
    assert json.loads(result) == {"success": True}

@patch("soft_ue_cli.client.call_tool")
@patch("soft_ue_cli.client.call_tool_ex")
def test_tool_call_stops_pie_on_start_timeout(mock_call_tool_ex, mock_call_tool):
    def start_side_effect(tool_name, arguments, timeout=None):
        raise BridgeError(
            kind=ErrorKind.EXPECTED,
            message="request timed out after 30s",
            tool_name=tool_name,
            arguments=arguments,
        )

    mock_call_tool_ex.side_effect = start_side_effect
    mock_call_tool.return_value = {"stopped": True}
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "pie-session":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(action="start", timeout=30)
    parsed = json.loads(result)
    assert parsed["error"] == "Tool 'pie-session' failed: request timed out after 30s"
    assert mock_call_tool_ex.call_count == 1
    assert mock_call_tool.call_count == 1
    assert mock_call_tool.call_args.args[0] == "pie-session"
    assert mock_call_tool.call_args.args[1]["action"] == "stop"

@patch("soft_ue_cli.client.call_tool_ex")
def test_tool_call_returns_json_string(mock_call_tool):
    mock_call_tool.return_value = ({"status": "ok"}, BridgeCallMeta())
    server = create_server()

    tool_fn = None
    for tool in server._tool_manager._tools.values():
        if tool.name == "query-level":
            tool_fn = tool.fn
            break

    assert tool_fn is not None
    result = tool_fn(limit=1)
    assert isinstance(result, str)
    parsed = json.loads(result)
    assert parsed == {"status": "ok"}

def test_client_tool_fn_handles_system_exit():
    def exiting_cmd(_args):
        raise SystemExit(1)

    tool_fn = _make_client_tool_fn("failing-tool", exiting_cmd, {})
    result = tool_fn()
    parsed = json.loads(result)
    assert parsed == {"error": "Command 'failing-tool' exited with code 1"}

def test_client_tool_fn_handles_exception():
    def failing_cmd(_args):
        raise RuntimeError("boom")

    tool_fn = _make_client_tool_fn("failing-tool", failing_cmd, {})
    result = tool_fn()
    parsed = json.loads(result)
    assert parsed == {"error": "Command 'failing-tool' failed: boom"}

def test_client_tool_fn_preserves_stderr_reason_on_failure():
    def boom(_namespace):
        print("error: asset not found: /Game/Nope", file=sys.stderr)
        raise SystemExit(1)

    fn = _make_client_tool_fn("cloth query", boom, {}, None)
    payload = fn()

    assert "asset not found" in payload

def test_prompt_list_has_blueprint_to_cpp():
    server = create_server()
    prompts = server._prompt_manager._prompts
    prompt_names = {p for p in prompts}
    assert "blueprint-to-cpp" in prompt_names
    assert "author-test" in prompt_names

def test_prompt_fn_returns_content():
    server = create_server()
    prompt = server._prompt_manager._prompts.get("blueprint-to-cpp")
    assert prompt is not None
    result = prompt.fn()
    assert isinstance(result, str)
    assert "Blueprint to C++" in result

def test_mcp_tool_result_carries_session_notices(monkeypatch):
    from soft_ue_cli import client as client_mod
    from soft_ue_cli import mcp_server

    meta = client_mod.BridgeCallMeta(
        notices=[{"kind": "notice", "from_label": "builder", "text": "started a rebuild"}]
    )
    monkeypatch.setattr(
        client_mod, "call_tool_ex", lambda *a, **kw: ({"actors": []}, meta)
    )

    fn = mcp_server._make_tool_fn("query-level", {"type": "object", "properties": {}})
    payload = json.loads(fn())

    assert payload["actors"] == []
    assert payload["session_notices"][0]["from_label"] == "builder"

def test_mcp_tool_result_omits_session_notices_when_empty(monkeypatch):
    from soft_ue_cli import client as client_mod
    from soft_ue_cli import mcp_server

    meta = client_mod.BridgeCallMeta(notices=[])
    monkeypatch.setattr(
        client_mod, "call_tool_ex", lambda *a, **kw: ({"actors": []}, meta)
    )

    fn = mcp_server._make_tool_fn("query-level", {"type": "object", "properties": {}})
    payload = json.loads(fn())

    assert "session_notices" not in payload

def test_mcp_tool_error_response_carries_session_notices(monkeypatch):
    from soft_ue_cli import client as client_mod
    from soft_ue_cli import mcp_server

    def raise_with_notices(*_args, **_kwargs):
        raise BridgeError(
            ErrorKind.EXPECTED,
            "cannot connect to SoftUEBridge",
            "query-level",
            {},
            notices=[{"kind": "notice", "from_label": "builder", "text": "rebuilding now"}],
        )

    monkeypatch.setattr(client_mod, "call_tool_ex", raise_with_notices)

    fn = mcp_server._make_tool_fn("query-level", {"type": "object", "properties": {}})
    payload = json.loads(fn())

    assert payload["session_notices"][0]["from_label"] == "builder"

def test_mcp_tool_call_does_not_inherit_a_pooled_threads_label(monkeypatch):
    """`session_as` is a model-reachable tool parameter on a long-lived process.

    FastMCP runs tools on pooled anyio worker threads, so a label left behind by
    an earlier `session announce` must not silently become the identity of the
    next `pie-session` or `build-and-relaunch` that lands on the same thread.
    """
    from soft_ue_cli import client as client_mod
    from soft_ue_cli import mcp_server

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    seen = {}

    def capture_identity(*_args, **_kwargs):
        seen["id"] = client_mod.session_descriptor()["id"]
        return {}, client_mod.BridgeCallMeta(notices=[])

    monkeypatch.setattr(client_mod, "call_tool_ex", capture_identity)
    client_mod.set_session_label("cape-cloth")  # an earlier call on this thread
    try:
        mcp_server._make_tool_fn("query-level", {"type": "object", "properties": {}})()
    finally:
        client_mod.clear_session_label()

    assert seen["id"].startswith("unknown:")

def test_mcp_client_tool_fn_does_not_inherit_a_pooled_threads_label(monkeypatch):
    """Same reset on the client-side path, which is where `session_as` arrives."""
    from soft_ue_cli import client as client_mod
    from soft_ue_cli import mcp_server

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    seen = {}

    def cmd_fn(_namespace):
        seen["id"] = client_mod.session_descriptor()["id"]

    fn = mcp_server._make_client_tool_fn(
        "skills list", cmd_fn, {"type": "object", "properties": {}}
    )
    client_mod.set_session_label("cape-cloth")  # an earlier call on this thread
    try:
        fn()
    finally:
        client_mod.clear_session_label()

    assert seen["id"].startswith("unknown:")

def test_create_server_sets_a_process_wide_default_label(monkeypatch):
    """The startup m-<uuid8> is a legitimate process-wide identity, not per-call state."""
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    client_mod.clear_session_label()

    create_server()

    assert client_mod._DEFAULT_SESSION_LABEL.startswith("m-")
    assert client_mod.session_descriptor()["id"] == client_mod._DEFAULT_SESSION_LABEL

def _patch_both_bridge_entrypoints(monkeypatch, seen: dict):
    """Intercept every bridge call an MCP tool can make, recording who it claims to be.

    The two families reach different attributes: `session announce` and `session
    list` run through `__main__._run_tool`, which bound `call_tool_ex` at import,
    while `pie-session` goes through the `_client.call_tool_ex` lookup in
    `mcp_server`. Patching only one leaves the other hitting a real editor.
    """
    from soft_ue_cli import __main__ as main_mod
    from soft_ue_cli import client as client_mod

    def recorder(kind: str):
        def call_tool_ex(*_args, **_kwargs):
            seen[kind] = client_mod.session_descriptor()["id"]
            return {}, BridgeCallMeta(notices=[])
        return call_tool_ex

    monkeypatch.setattr(main_mod, "call_tool_ex", recorder("session"))
    monkeypatch.setattr(client_mod, "call_tool_ex", recorder("bridge"))

def test_announced_mcp_agent_keeps_one_roster_row(monkeypatch):
    """An MCP agent has no shell, so `session_as` on `announce` is the only place
    it can say its name. If that name does not outlive the announce call, its next
    `pie-session start` claims `pie` under the startup `m-<uuid8>` row instead:
    two rows for one agent, and the one holding `pie` is the nameless one.
    """
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    client_mod.clear_session_label()

    seen: dict = {}
    _patch_both_bridge_entrypoints(monkeypatch, seen)

    tools = create_server()._tool_manager._tools
    assert client_mod._DEFAULT_SESSION_LABEL.startswith("m-")

    try:
        tools["session announce"].fn(session_as="cape-cloth", status="Converting SK_Cape")
        tools["pie-session"].fn(action="start")
    finally:
        client_mod.clear_session_label()

    assert seen["session"] == "cape-cloth"
    assert seen["bridge"] == "cape-cloth"

def test_per_call_as_outside_announce_does_not_become_the_default(monkeypatch):
    """`--as` on `list`/`ask`/`inbox`/... means "act as", for that call only.

    Only `announce` declares a durable identity, so a one-off `session list
    --as other` must leave the process default — and every later call — alone.
    """
    from soft_ue_cli import client as client_mod

    monkeypatch.delenv("SOFT_UE_SESSION", raising=False)
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    client_mod.clear_session_label()

    seen: dict = {}
    _patch_both_bridge_entrypoints(monkeypatch, seen)

    tools = create_server()._tool_manager._tools
    startup_label = client_mod._DEFAULT_SESSION_LABEL

    try:
        tools["session list"].fn(session_as="other")
        tools["pie-session"].fn(action="stop")
    finally:
        client_mod.clear_session_label()

    assert seen["session"] == "other"
    assert client_mod._DEFAULT_SESSION_LABEL == startup_label
    assert seen["bridge"] == startup_label

def test_announce_does_not_promote_the_env_derived_default(monkeypatch):
    """FastMCP passes every parameter, filling omitted ones from the schema default.

    `session_as` defaults to `$SOFT_UE_SESSION`, so an announce with no name of its
    own still arrives carrying that value. Promoting it would mean "the caller
    declared an identity" whenever the env happens to be set.
    """
    from soft_ue_cli import client as client_mod

    monkeypatch.setenv("SOFT_UE_SESSION", "envname")
    # Patched twice: this one restores the real original at teardown, since
    # create_server() overwrites the global before the second patch records it.
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", None)
    seen: dict = {}
    _patch_both_bridge_entrypoints(monkeypatch, seen)

    tools = create_server()._tool_manager._tools
    monkeypatch.setattr(client_mod, "_DEFAULT_SESSION_LABEL", "m-fixed")
    client_mod.clear_session_label()

    try:
        tools["session announce"].fn(session_as="envname", status="idle")
    finally:
        client_mod.clear_session_label()

    assert client_mod._DEFAULT_SESSION_LABEL == "m-fixed"

def test_client_tool_fn_injects_session_notices_from_run_tool(monkeypatch):
    """A space-named tool (routed through _make_client_tool_fn) whose cmd_fn calls
    the real _run_tool must surface notices in the returned body, even though
    _run_tool itself only ever returns a bare dict (global constraint)."""
    from soft_ue_cli import __main__ as main_mod
    from soft_ue_cli.client import BridgeCallMeta

    meta = BridgeCallMeta(
        notices=[{"kind": "notice", "from_label": "builder", "text": "started a rebuild"}]
    )
    monkeypatch.setattr(main_mod, "call_tool_ex", lambda *a, **kw: ({"success": True}, meta))

    def cmd_fn(_namespace):
        main_mod._print_json(main_mod._run_tool("cloth-weld", {}))

    fn = _make_client_tool_fn("cloth weld", cmd_fn, {})
    payload = json.loads(fn())

    assert payload["success"] is True
    assert payload["session_notices"][0]["from_label"] == "builder"

def test_client_tool_fn_omits_session_notices_when_run_tool_has_none(monkeypatch):
    from soft_ue_cli import __main__ as main_mod
    from soft_ue_cli.client import BridgeCallMeta

    meta = BridgeCallMeta(notices=[])
    monkeypatch.setattr(main_mod, "call_tool_ex", lambda *a, **kw: ({"success": True}, meta))

    def cmd_fn(_namespace):
        main_mod._print_json(main_mod._run_tool("cloth-weld", {}))

    fn = _make_client_tool_fn("cloth weld", cmd_fn, {})
    payload = json.loads(fn())

    assert payload == {"success": True}
    assert "session_notices" not in payload

def test_client_tool_fn_leaves_non_json_output_untouched_with_notices():
    from soft_ue_cli import client as client_mod

    def cmd_fn(_namespace):
        client_mod.record_notices([{"kind": "notice", "from_label": "builder", "text": "fyi"}])
        print("plain text output")

    fn = _make_client_tool_fn("cloth weld", cmd_fn, {})
    result = fn()

    assert result == "plain text output"
