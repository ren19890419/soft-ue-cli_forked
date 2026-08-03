"""MCP server mode for soft-ue-cli.

Exposes CLI commands as MCP tools and skills as MCP prompts
over stdio transport using FastMCP.

The ``mcp`` package is imported at call time so this module
can be imported without mcp installed (for testing/introspection).
"""

from __future__ import annotations

import inspect
import io
import json
import os
import sys
from typing import Any, List, Optional

import argparse as _argparse

from . import client as _client
from . import streak as _streak
from .errors import BridgeError, ErrorKind, bug_nudge_payload
from .mcp_schema import CLIENT_SIDE_COMMANDS, extract_tools
from .skills import get_skill, list_skills

# CLI command names that differ from their bridge tool names.
_BRIDGE_TOOL_NAME_MAP: dict[str, str] = {
    "capture-pie-screenshot": "capture-screenshot",
    "class-hierarchy": "get-class-hierarchy",
    "project-info": "get-project-info",
}


_BRIDGE_TOOL_DEFAULTS: dict[str, dict[str, Any]] = {
    "capture-pie-screenshot": {"mode": "pie-window"},
}

# Per-tool parameter renames: MCP exposes argparse dest names, but some bridge
# tools expect different key names. Map (tool_name → {mcp_param: bridge_param}).
_PARAM_RENAMES: dict[str, dict[str, str]] = {
    "query-asset": {"asset_class": "class"},
    "exec-console-command": {"command_parts": "command"},
}

_JSON_TYPE_TO_PY: dict[str, type] = {
    "string": str,
    "boolean": bool,
    "integer": int,
    "number": float,
    "array": list,
    "object": dict,
}


def _build_signature(params: dict | None) -> inspect.Signature:
    """Build a typed inspect.Signature from a tool's JSON Schema parameters dict.

    FastMCP introspects __signature__ to generate the MCP tool's JSON schema,
    so each parameter must be a proper named, typed, keyword-only Parameter.

    Special types beyond standard JSON Schema:
      "any"   — uses typing.Any so pydantic accepts any JSON value
      "array" — uses list so pydantic accepts JSON arrays
    """
    if not params:
        return inspect.Signature([])
    properties = params.get("properties", {})
    required_params = set(params.get("required", []))
    sig_params: list[inspect.Parameter] = []
    for param_name, prop in properties.items():
        json_type = prop.get("type", "string")
        if json_type == "any":
            # Accept any JSON value — no pydantic type constraint
            if param_name in required_params:
                annotation: Any = Any
                default = inspect.Parameter.empty
            else:
                annotation = Optional[Any]
                default = prop.get("default", None)
        else:
            py_type = _JSON_TYPE_TO_PY.get(json_type, str)
            if param_name in required_params:
                annotation = py_type
                default = inspect.Parameter.empty
            else:
                annotation = Optional[py_type]
                default = prop.get("default", None)
        sig_params.append(
            inspect.Parameter(
                param_name,
                inspect.Parameter.KEYWORD_ONLY,
                default=default,
                annotation=annotation,
            )
        )
    return inspect.Signature(sig_params)


def _normalize_add_graph_node_result(result: dict[str, Any]) -> dict[str, Any]:
    """Normalize add-graph-node results to always include ``node_guid`` when available."""
    if not isinstance(result, dict):
        return result

    node_guid = result.get("node_guid")
    if isinstance(node_guid, str) and node_guid:
        return result

    # Blueprint tools return ``created_nodes`` for special cases (e.g. AnimLayerFunction),
    # so extract the first available guid for compatibility.
    candidates: list[Any] = []
    created_nodes = result.get("created_nodes")
    if isinstance(created_nodes, list):
        candidates.extend(created_nodes)

    if not isinstance(node_guid, str):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            for guid_key in ("node_guid", "guid"):
                value = candidate.get(guid_key)
                if isinstance(value, str) and value:
                    result["node_guid"] = value
                    return result

    return result


def _make_tool_fn(tool_name: str, params: dict | None = None):
    """Create a bridge tool handler that forwards kwargs to call_tool()."""
    bridge_name = _BRIDGE_TOOL_NAME_MAP.get(tool_name, tool_name)

    def tool_fn(**kwargs: Any) -> str:
        # Worker threads are pooled: drop any label a previous tool call left on
        # this one, so identity never outlives the call that declared it.
        _client.clear_session_label()

        # Filter out None and falsey defaults for store_true-style args, while
        # keeping explicit values that must be translated into bridge args.
        arguments = {k: v for k, v in kwargs.items() if v is not None}

        # MCP exposes argparse dest names (no_auto_position). The bridge expects
        # auto_position=true/false, with false meaning "disable auto placement".
        if arguments.pop("no_auto_position", False):
            arguments["auto_position"] = False

        # Apply any per-tool parameter renames. If MCP callers provide both the
        # explicit canonical field and a legacy alias, keep the explicit field.
        for mcp_name, bridge_name_param in _PARAM_RENAMES.get(tool_name, {}).items():
            if mcp_name in arguments and bridge_name_param not in arguments:
                arguments[bridge_name_param] = arguments.pop(mcp_name)
            else:
                arguments.pop(mcp_name, None)
        for default_name, default_value in _BRIDGE_TOOL_DEFAULTS.get(tool_name, {}).items():
            arguments.setdefault(default_name, default_value)

        if tool_name == "exec-console-command" and isinstance(arguments.get("command"), (list, tuple)):
            arguments["command"] = " ".join(str(part) for part in arguments["command"])

        # Use tool-level timeout as HTTP timeout for pie-session start, to avoid
        # MCP request timeout when PIE warmup needs longer.
        http_timeout = None
        if tool_name == "pie-session" and arguments.get("action") == "start":
            timeout_arg = arguments.get("timeout")
            if timeout_arg is not None:
                try:
                    http_timeout = float(timeout_arg)
                except (TypeError, ValueError):
                    http_timeout = None
        elif tool_name == "pie-tick":
            try:
                http_timeout = float(arguments.get("timeout", 30.0)) + 60.0
            except (TypeError, ValueError):
                http_timeout = 90.0

        try:
            result, meta = _client.call_tool_ex(bridge_name, arguments, timeout=http_timeout)
        except BridgeError as exc:
            # If PIE startup times out, attempt a best-effort stop to avoid leaving
            # the editor in a partially-initialized session state.
            if tool_name == "pie-session" and arguments.get("action") == "start":
                if "timed out" in exc.message.lower():
                    try:
                        _client.call_tool("pie-session", {"action": "stop"}, timeout=5.0)
                    except Exception:
                        pass
            error_response = {"error": f"Tool '{tool_name}' failed: {exc.message}"}
            if exc.kind == ErrorKind.UNEXPECTED:
                error_response["bug_report_hint"] = bug_nudge_payload(
                    exc.tool_name, exc.message,
                )
            if exc.notices:
                error_response["session_notices"] = exc.notices
            return json.dumps(error_response, indent=2, ensure_ascii=False)

        if tool_name == "add-graph-node":
            try:
                result = _normalize_add_graph_node_result(result)
            except Exception as exc:
                return json.dumps({"error": f"Tool '{tool_name}' failed: {exc}"}, indent=2, ensure_ascii=False)

        # Record streak (best-effort)
        try:
            _streak.record_success(tool_name)
            if _streak.should_nudge_testimonial():
                result["testimonial_nudge"] = {
                    "message": (
                        "You've been using soft-ue-cli successfully for 3+ days! "
                        "If it's been helpful, consider sharing a testimonial."
                    ),
                    "suggested_command": "submit-testimonial",
                    "suggested_args": {
                        "message": "Share what you liked about soft-ue-cli",
                        "agent-name": "your agent name",
                    },
                }
                _streak.mark_nudged()
        except Exception:
            pass

        if meta.notices:
            result["session_notices"] = meta.notices

        return json.dumps(result, indent=2, ensure_ascii=False)

    tool_fn.__name__ = tool_name.replace(" ", "__").replace("-", "_")
    tool_fn.__qualname__ = tool_fn.__name__
    tool_fn.__signature__ = _build_signature(params)

    return tool_fn


def _make_client_tool_fn(tool_name: str, cmd_fn, params: dict | None = None, defaults: dict | None = None):
    """Create a client-side tool handler that runs the existing cmd_* function.

    Captures stdout so the output reaches the MCP client instead of the terminal.
    SystemExit (from error paths in cmd_* handlers) is caught and reported cleanly.
    """

    def tool_fn(**kwargs: Any) -> str:
        option_defaults = {
            name: prop.get("default")
            for name, prop in (params or {}).get("properties", {}).items()
        }
        namespace = _argparse.Namespace(**{**option_defaults, **(defaults or {}), **kwargs})
        if tool_name == "config" and hasattr(namespace, "subcommand") and not hasattr(namespace, "config_action"):
            namespace.config_action = namespace.subcommand
        buffer = io.StringIO()
        err_buffer = io.StringIO()
        old_stdout = sys.stdout
        old_stderr = sys.stderr
        sys.stdout = buffer
        sys.stderr = err_buffer
        output = ""
        # Same pooled-thread reset as _make_tool_fn: `session_as` reaches here as
        # a tool parameter, and must not retarget any later call on this thread.
        _client.clear_session_label()
        # `announce` is the one leaf whose name means "this is who I am from here
        # on" rather than "act as, for this call". An MCP agent has no shell to put
        # SOFT_UE_SESSION in front of every command, so without this promotion its
        # announce lands on the readable row and its `pie-session start` on the
        # startup m-<uuid8> row. FastMCP passes every parameter, filled from the
        # schema default when the caller omitted it, so an explicit value is one
        # that differs from that default — never the $SOFT_UE_SESSION fallback.
        if tool_name == "session announce":
            declared = kwargs.get("session_as")
            if declared and declared != option_defaults.get("session_as"):
                _client.set_default_session_label(declared)
        _client.begin_notice_capture()
        try:
            cmd_fn(namespace)
            output = buffer.getvalue().strip()
            if tool_name == "blueprint node add" and output:
                normalized = _normalize_add_graph_node_result(json.loads(output))
                output = json.dumps(normalized, indent=2, ensure_ascii=False)
        except SystemExit as exc:
            output = buffer.getvalue().strip()
            reason = err_buffer.getvalue().strip()
            _client.take_captured_notices()
            return output or json.dumps(
                {"error": reason or f"Command '{tool_name}' exited with code {exc.code}"},
                indent=2,
            )
        except Exception as exc:
            output = buffer.getvalue().strip()
            reason = err_buffer.getvalue().strip()
            _client.take_captured_notices()
            return output or json.dumps(
                {"error": reason or f"Command '{tool_name}' failed: {exc}"},
                indent=2,
            )
        finally:
            sys.stdout = old_stdout
            sys.stderr = old_stderr

        output = output or buffer.getvalue().strip()
        notices = _client.take_captured_notices()
        if notices:
            try:
                payload = json.loads(output) if output else None
            except (json.JSONDecodeError, TypeError):
                payload = None
            if isinstance(payload, dict):
                payload["session_notices"] = notices
                output = json.dumps(payload, indent=2, ensure_ascii=False)
        return output or json.dumps({"status": "ok"}, indent=2)

    tool_fn.__name__ = tool_name.replace(" ", "__").replace("-", "_")
    tool_fn.__qualname__ = tool_fn.__name__
    tool_fn.__signature__ = _build_signature(params)

    return tool_fn


def create_server():
    """Create and configure the MCP server with all tools and prompts."""
    from mcp.server.fastmcp import FastMCP
    from mcp.server.fastmcp.prompts import Prompt

    import uuid

    _client.set_client_kind("mcp")
    _client.set_default_session_label(
        os.environ.get("SOFT_UE_SESSION") or f"m-{uuid.uuid4().hex[:8]}"
    )

    mcp = FastMCP("soft-ue-cli")

    # Register tools from argparse introspection
    for tool_def in extract_tools():
        name = tool_def["name"]
        description = tool_def["description"]
        params = tool_def["parameters"]

        if name in CLIENT_SIDE_COMMANDS or " " in name:
            fn = _make_client_tool_fn(name, tool_def["func"], params, tool_def.get("defaults"))
        else:
            fn = _make_tool_fn(name, params)
        fn.__doc__ = description

        mcp.add_tool(fn, name=name, description=description)

    # Register skills as prompts
    for skill in list_skills():
        skill_name = skill["name"]
        skill_desc = skill["description"]

        def make_prompt_fn(sn: str, sd: str):
            def prompt_fn() -> str:
                content = get_skill(sn)
                return content or f"Skill '{sn}' not found"
            prompt_fn.__name__ = sn.replace("-", "_")
            prompt_fn.__qualname__ = prompt_fn.__name__
            prompt_fn.__doc__ = sd
            return prompt_fn

        prompt = Prompt.from_function(
            make_prompt_fn(skill_name, skill_desc),
            name=skill_name,
            description=skill_desc,
        )
        mcp.add_prompt(prompt)

    return mcp


def run_server() -> None:
    """Run the MCP server over stdio transport."""
    server = create_server()
    server.run(transport="stdio")
