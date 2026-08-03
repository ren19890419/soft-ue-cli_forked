"""HTTP/JSON-RPC client for the SoftUEBridge server."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .discovery import get_forced_port_fallback_url, get_server_url

_id_counter = itertools.count(1)

_DEFAULT_SESSION_LABEL: str | None = None
_CLIENT_KIND: str = "cli"

_session_identity = threading.local()


def set_session_label(label: str | None) -> None:
    """Declare the calling thread's session name. Ignored when label is empty.

    Thread-scoped on purpose. The CLI is one process per command, but the MCP
    server is one long-lived process whose tool calls run on pooled anyio worker
    threads, and the label is model-reachable there as a tool parameter. A
    process global would let a single `session announce(session_as=...)` retarget
    the identity of every later bridge call from that server, with no way back.
    """
    if label:
        _session_identity.label = label


def clear_session_label() -> None:
    """Forget the calling thread's session name.

    Worker threads are pooled and reused, so a surface that accepts a per-call
    label must clear it before the next call lands on the same thread.
    """
    _session_identity.label = None


def set_default_session_label(label: str | None) -> None:
    """Set the process-wide fallback name, used when no thread declared one.

    This is for a genuine process-wide identity — the MCP server's startup
    `m-<uuid8>` — not for per-call state.
    """
    global _DEFAULT_SESSION_LABEL
    if label:
        _DEFAULT_SESSION_LABEL = label


def set_client_kind(kind: str) -> None:
    """Record which surface is calling: 'cli' or 'mcp'."""
    global _CLIENT_KIND
    _CLIENT_KIND = kind


_notice_sink = threading.local()


def begin_notice_capture() -> None:
    """Start collecting notices for the current thread. Clears anything prior."""
    _notice_sink.notices = []


def record_notices(notices: list[dict[str, Any]]) -> None:
    """Add notices to this thread's capture, if one is active."""
    existing = getattr(_notice_sink, "notices", None)
    if existing is not None and notices:
        existing.extend(notices)


def take_captured_notices() -> list[dict[str, Any]]:
    """Return and clear this thread's captured notices."""
    captured = getattr(_notice_sink, "notices", None) or []
    _notice_sink.notices = None
    return list(captured)


def _origin_id() -> str:
    """Stable 8-char id for this working directory (distinguishes worktrees)."""
    resolved = str(Path.cwd().resolve())
    return hashlib.sha1(resolved.encode("utf-8")).hexdigest()[:8]


def session_descriptor() -> dict[str, Any]:
    """Identity block sent as params._session on every bridge call.

    Resolution order: this thread's set_session_label() >
    set_default_session_label() > SOFT_UE_SESSION env > derived.
    A derived identity is deliberately unaddressable: it marks presence
    without claiming a name anyone can reply to.
    """
    label = (
        getattr(_session_identity, "label", None)
        or _DEFAULT_SESSION_LABEL
        or os.environ.get("SOFT_UE_SESSION")
        or ""
    )
    origin = _origin_id()
    if label:
        return {
            "id": label,
            "label": label,
            "origin": origin,
            "client": _CLIENT_KIND,
            "pid": os.getpid(),
            "confidence": "declared",
        }
    return {
        "id": f"unknown:{origin}",
        "label": "",
        "origin": origin,
        "client": _CLIENT_KIND,
        "pid": os.getpid(),
        "confidence": "derived",
    }


def _forced_port_fallback_warning(original_url: str, fallback_url: str) -> str:
    return (
        "SOFT_UE_BRIDGE_PORT appears stale: "
        f"{original_url} is unreachable or not a SoftUEBridge server, "
        f"using live bridge from .soft-ue-bridge/instance.json at {fallback_url}."
    )


def _handle_startup_recovery_for_connection() -> str | None:
    """Handle an Unreal startup recovery prompt before retrying a failed connection."""
    from .startup_recovery import handle_startup_recovery_prompt

    interactive = sys.stdin.isatty()
    requested_action = "ask" if interactive else "remembered"
    result = handle_startup_recovery_prompt(requested_action, interactive=interactive)
    if result is None:
        return None
    if result.action == "manual":
        return (
            "Unreal Editor startup recovery prompt is visible. Choose in the editor or rerun a "
            "launch workflow with --startup-recovery recover|skip --remember-startup-recovery."
        )
    return f"handled Unreal startup recovery prompt with action '{result.action}'"


def _find_bridge_dir(start: Path | None = None) -> Path | None:
    """Walk up from start (or cwd) looking for a .soft-ue-bridge directory."""
    current = (start or Path.cwd()).resolve()
    for directory in [current, *current.parents]:
        candidate = directory / ".soft-ue-bridge"
        if candidate.is_dir():
            return candidate
    return None


# A shutdown_intent older than this is not evidence about the disconnect in
# hand. Matches FBridgeSessionRegistry::Rehydrate, which drops records whose
# last_seen_utc is over 30 minutes old.
_SHUTDOWN_INTENT_MAX_AGE_S = 30 * 60


def _parse_utc(value: Any) -> datetime | None:
    """Parse an FDateTime::ToIso8601 string. Returns None for anything else.

    Deliberately total: callers turn "cannot tell" into a decision, never into
    an exception, because the only caller runs inside an error path.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _record_age_s(written_utc: Any) -> float | None:
    """Seconds since sessions.json was written, or None when unknowable."""
    written = _parse_utc(written_utc)
    if written is None:
        return None
    return (datetime.now(timezone.utc) - written).total_seconds()


def session_postmortem(project_dir: Path | None = None) -> str:
    """Explain a vanished editor using the last session records on disk.

    The editor writes .soft-ue-bridge/sessions.json (via FBridgeSessionRegistry::
    Flush) on every session mutation, and flushes it once more with a
    shutdown_intent entry before build-and-relaunch asks the editor to exit.
    That file is what survives the editor's death, so it is what this reads.

    A shutdown_intent outlives the editor that wrote it: Rehydrate restores only
    the sessions array, and Touch/ClaimResource/StartServer never flush, so an
    intent from a previous editor can still be on disk when this editor is
    taskkilled or crashes. Naming that session as the cause would be confidently
    wrong. So the attribution is shown only when the file's own written_utc
    proves it is recent; otherwise this degrades to the last-known-sessions line.

    Returns "" when nothing useful is known. Never raises: this runs inside
    an error path and must not replace one failure with another. That covers
    not just a missing or unparseable file, but valid JSON in an unexpected
    shape (e.g. a future format change, or a file caught mid-write) — every
    field access below is guarded by the outer try and by isinstance checks,
    not just the json.loads() call.
    """
    try:
        bridge_dir = _find_bridge_dir(project_dir)
        if bridge_dir is None:
            return ""
        sessions_path = bridge_dir / "sessions.json"
        if not sessions_path.exists():
            return ""
        data = json.loads(sessions_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return ""

        lines = []
        age_s = _record_age_s(data.get("written_utc"))
        intent_is_recent = age_s is not None and age_s <= _SHUTDOWN_INTENT_MAX_AGE_S
        intent = data.get("shutdown_intent")
        if intent_is_recent and isinstance(intent, dict):
            who = intent.get("from_label") or intent.get("from")
            if who:
                when = intent.get("created_utc", "an unknown time")
                what = intent.get("text", "shut the editor down")
                lines.append(f"  The editor is gone. {who} {what} at {when}.")

        try:
            status_path = bridge_dir.parent / "Saved" / "Temp" / "BuildAndRelaunch.status.json"
            if status_path.exists():
                status = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(status, dict):
                    stage = status.get("stage", "unknown")
                    lines.append(f"  Build status: {stage} ({status.get('build_log_path', '')})")
        except Exception:
            pass

        if not lines:
            sessions = data.get("sessions", [])
            others = [
                s.get("label") or s.get("id")
                for s in sessions
                if isinstance(s, dict) and (s.get("label") or s.get("id"))
            ] if isinstance(sessions, list) else []
            if others:
                lines.append(f"  Last known sessions in this project: {', '.join(others)}")

        return "\n".join(lines)
    except Exception:
        return ""


@dataclass
class BridgeCallMeta:
    """Out-of-band data that rode along with a bridge response."""

    notices: list[dict[str, Any]] = field(default_factory=list)


def call_tool_ex(
    tool_name: str, arguments: dict[str, Any], timeout: float | None = None
) -> tuple[dict[str, Any], BridgeCallMeta]:
    """Call a tool on the SoftUEBridge server and return the parsed result plus call metadata.

    Raises BridgeError on connection errors or tool errors.
    """
    from .errors import BridgeError, ErrorKind

    url = get_server_url()
    endpoint = f"{url}/bridge"
    timeout = timeout if timeout is not None else float(os.environ.get("SOFT_UE_BRIDGE_TIMEOUT", "30"))

    payload = {
        "jsonrpc": "2.0",
        "id": str(next(_id_counter)),
        "method": "tools/call",
        "params": {
            "name": tool_name,
            "arguments": arguments,
            "_session": session_descriptor(),
        },
    }

    def post_once(target_endpoint: str) -> httpx.Response:
        response = httpx.post(target_endpoint, json=payload, timeout=timeout)
        response.raise_for_status()
        return response

    def bridge_error_from_http(exc: httpx.HTTPStatusError) -> BridgeError:
        kind = ErrorKind.UNEXPECTED if exc.response.status_code >= 500 else ErrorKind.EXPECTED
        return BridgeError(
            kind=kind,
            message=f"HTTP {exc.response.status_code}",
            tool_name=tool_name,
            arguments=arguments,
        )

    def connection_message(exc: httpx.ConnectError | httpx.TimeoutException, note: str | None = None) -> str:
        if isinstance(exc, httpx.ConnectError):
            message = (
                f"cannot connect to SoftUEBridge at {endpoint}\n"
                "Make sure the plugin is enabled and the game is running."
            )
            if postmortem := session_postmortem():
                message += "\n" + postmortem
        else:
            message = (
                f"request timed out after {timeout:.0f}s\n"
                "Possible causes:\n"
                "  - A modal dialog may be blocking the UE editor (check for popups)\n"
                "  - The operation is slow (set SOFT_UE_BRIDGE_TIMEOUT=<seconds>)"
            )
        if note:
            message += f"\n{note}"
        return message

    def recover_or_raise(exc: httpx.ConnectError | httpx.TimeoutException) -> httpx.Response:
        recovery_note = None
        try:
            recovery_note = _handle_startup_recovery_for_connection()
        except Exception as recovery_exc:
            recovery_note = str(recovery_exc)

        if recovery_note and recovery_note.startswith("handled "):
            try:
                return post_once(endpoint)
            except (httpx.ConnectError, httpx.TimeoutException) as retry_exc:
                raise BridgeError(
                    kind=ErrorKind.EXPECTED,
                    message=connection_message(retry_exc, recovery_note),
                    tool_name=tool_name,
                    arguments=arguments,
                )
            except httpx.HTTPStatusError as retry_exc:
                raise bridge_error_from_http(retry_exc)

        raise BridgeError(
            kind=ErrorKind.EXPECTED,
            message=connection_message(exc, recovery_note),
            tool_name=tool_name,
            arguments=arguments,
        )

    def forced_port_fallback_response() -> httpx.Response | None:
        fallback_url = get_forced_port_fallback_url(url)
        if not fallback_url:
            return None
        fallback_endpoint = f"{fallback_url}/bridge"
        response = post_once(fallback_endpoint)
        print(_forced_port_fallback_warning(url, fallback_url), file=sys.stderr)
        return response

    try:
        response = post_once(endpoint)
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        try:
            response = forced_port_fallback_response()
        except (httpx.ConnectError, httpx.TimeoutException):
            response = None
        except httpx.HTTPStatusError as fallback_http_exc:
            raise bridge_error_from_http(fallback_http_exc)
        if response is None:
            response = recover_or_raise(exc)
    except httpx.HTTPStatusError as exc:
        try:
            response = forced_port_fallback_response()
        except (httpx.ConnectError, httpx.TimeoutException):
            response = None
        except httpx.HTTPStatusError as fallback_http_exc:
            raise bridge_error_from_http(fallback_http_exc)
        if response is None:
            raise bridge_error_from_http(exc)

    try:
        data = response.json()
    except Exception:
        raise BridgeError(
            kind=ErrorKind.UNEXPECTED,
            message="server returned non-JSON response",
            tool_name=tool_name,
            arguments=arguments,
        )

    if "error" in data:
        err = data["error"]
        raise BridgeError(
            kind=ErrorKind.UNEXPECTED,
            message=str(err.get("message", err)),
            tool_name=tool_name,
            arguments=arguments,
        )

    result = data.get("result", {})
    meta = BridgeCallMeta(notices=list(result.get("session_notices") or []))
    if result.get("isError"):
        content = result.get("content", [])
        msg = content[0].get("text", "unknown error") if content else "unknown error"
        raise BridgeError(
            kind=ErrorKind.UNEXPECTED,
            message=msg,
            tool_name=tool_name,
            arguments=arguments,
            notices=meta.notices,
        )

    # Parse text content as JSON when possible
    content = result.get("content", [])
    if content and content[0].get("type") == "text":
        text = content[0].get("text", "")
        try:
            return json.loads(text), meta
        except json.JSONDecodeError:
            return {"text": text}, meta

    return result, meta


def call_tool(
    tool_name: str, arguments: dict[str, Any], timeout: float | None = None
) -> dict[str, Any]:
    """Call a tool on the SoftUEBridge server and return the parsed result.

    Raises BridgeError on connection errors or tool errors.
    """
    payload, _ = call_tool_ex(tool_name, arguments, timeout)
    return payload


def health_check(timeout: float = 5.0) -> dict[str, Any]:
    """GET /bridge health check."""
    url = get_server_url()

    def fallback_health_result() -> dict[str, Any] | None:
        fallback_url = get_forced_port_fallback_url(url)
        if not fallback_url:
            return None
        response = httpx.get(f"{fallback_url}/bridge", timeout=5.0)
        response.raise_for_status()
        result = response.json()
        result["warning"] = _forced_port_fallback_warning(url, fallback_url)
        result["bridge_url"] = fallback_url
        return result

    try:
        response = httpx.get(f"{url}/bridge", timeout=timeout)
        response.raise_for_status()
        return response.json()
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        try:
            if result := fallback_health_result():
                return result
        except Exception:
            pass
        return {"error": str(exc)}
    except httpx.HTTPStatusError as exc:
        try:
            if result := fallback_health_result():
                return result
        except Exception:
            pass
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": str(exc)}
