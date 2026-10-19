"""MCP server (stdio) exposing A2A peers as tools: list_peers, list_sessions, ask_peer, forget_session.

stdout is reserved for the MCP protocol; all logging goes to stderr.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from typing import TYPE_CHECKING, Any

from mcp.server.mcpserver import MCPServer

from a2a_peers import registry, resolver, state
from a2a_peers.registry import Peer, RegistryError
from a2a_peers.resolver import AmbiguousSession, SessionNotFound
from a2a_peers.state import SessionRef

if TYPE_CHECKING:
    from a2a_peers.a2a_client import A2AClient, AgentCard, AskResult

log = logging.getLogger("a2a_peers.mcp")

CARD_TIMEOUT_SECONDS = 5.0
LIST_TIMEOUT_SECONDS = 30.0
STALE_OPENCODE_ERROR = "UPSTREAM_RESOURCE_NOT_FOUND"
GUARD = "Only call when the user explicitly asks to contact another agent/session."

INSTRUCTIONS = """\
Tools for talking to other AI agents ("peers") over the A2A protocol, addressed by peer name and session name.
Only use them when the user explicitly asks to contact another agent or session.
- list_peers: show configured peers, whether they are reachable, and what they can do.
- list_sessions(peer): show session names known for a peer (cached names and, for opencode peers, titled sessions).
- ask_peer(peer, session, message): send a message to a named session on a peer and return its reply.
  For opencode peers the session must already exist with that exact title in the peer's TUI.
  For other peers an unknown session name starts a new conversation and remembers it.
  If the peer needs a permission approval, the reply says so; the user approves it in the peer's TUI, then ask again.
- forget_session(peer, session): drop the cached mapping for a session name.
Results starting with "ERROR:" are failures; relay them to the user instead of retrying blindly.
"""

server = MCPServer("a2a-peers", instructions=INSTRUCTIONS)


def client_factory(peer: Peer) -> A2AClient:
    """Create an A2A client for `peer`. Tests monkeypatch this."""
    from a2a_peers import a2a_client  # lazy: keeps this module importable without the protocol layer

    return a2a_client.A2AClient(peer.url, peer.token)


def _error(message: str) -> str:
    return f"ERROR: {message}"


def _describe_exc(exc: BaseException) -> str:
    text = str(exc).strip()
    return text or type(exc).__name__


def _is_a2a_error(exc: BaseException) -> bool:
    return any(cls.__name__ == "A2AError" for cls in type(exc).__mro__)


# --------------------------------------------------------------------------- list_peers


async def _peer_summary(peer: Peer) -> str:
    lines = [f"- {peer.name}: {peer.url}"]
    try:
        async with client_factory(peer) as client:
            card = await asyncio.wait_for(client.get_card(), CARD_TIMEOUT_SECONDS)
    except TimeoutError:
        lines.append(f"  reachable: no (no agent card within {CARD_TIMEOUT_SECONDS:g}s)")
        return "\n".join(lines)
    except Exception as exc:
        lines.append(f"  reachable: no ({_describe_exc(exc)})")
        return "\n".join(lines)
    lines.append("  reachable: yes")
    lines.append(f"  agent: {card.name}" + (f" - {card.description}" if card.description else ""))
    lines.append(f"  opencode: {'yes' if card.is_opencode else 'no'}")
    skills = [s.get("name") or s.get("id") for s in card.skills if isinstance(s, dict)]
    skills = [str(s) for s in skills if s]
    lines.append(f"  skills: {', '.join(skills) if skills else '(none)'}")
    return "\n".join(lines)


@server.tool(description=f"{GUARD} Lists configured A2A peers with reachability, agent name and skills.")
async def list_peers() -> str:
    try:
        peers = registry.load_peers()
    except Exception as exc:
        return _error(f"cannot load peer registry: {_describe_exc(exc)}")
    if not peers:
        return "No peers configured. Add one with `a2a-peers launch ...`."
    summaries = await asyncio.gather(*(_peer_summary(p) for p in peers.values()))
    return "Peers:\n" + "\n".join(summaries)


# --------------------------------------------------------------------------- list_sessions


@server.tool(
    description=f"{GUARD} Lists known session names for a peer (cached names and opencode session titles)."
)
async def list_sessions(peer: str) -> str:
    try:
        p = registry.get_peer(peer)
    except RegistryError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"cannot load peer registry: {_describe_exc(exc)}")
    lines = [f"Sessions for peer '{peer}':"]
    try:
        cached = state.list_cached(peer)
    except Exception as exc:
        lines.append(f"Cached names: (cannot read session cache: {_describe_exc(exc)})")
        cached = None
    if cached:
        lines.append("Cached names:")
        lines.extend(f"- {name} -> {ref.kind} {ref.id}" for name, ref in cached.items())
    elif cached is not None:
        lines.append("Cached names: (none)")
    try:
        async with client_factory(p) as client:
            card = await asyncio.wait_for(client.get_card(), CARD_TIMEOUT_SECONDS)
            if card.is_opencode:
                items = await asyncio.wait_for(client.list_opencode_sessions(limit=50), LIST_TIMEOUT_SECONDS)
                cached_titles = {
                    ref.id: name
                    for name, ref in (cached or {}).items()
                    if ref.kind == "opencode_session"
                }
                lines.append("Opencode sessions (address titled ones by exact title):")
                if not items:
                    lines.append("- (none)")
                for item in items:
                    title = item.get("title")
                    sid = item.get("id")
                    if not (isinstance(title, str) and title.strip()) and isinstance(sid, str):
                        title = cached_titles.get(sid)
                    label = repr(title) if isinstance(title, str) and title.strip() else "(untitled)"
                    lines.append(f"- {label} id={item.get('id')}")
    except TimeoutError:
        lines.append("(peer did not answer in time)")
    except Exception as exc:
        lines.append(f"(could not query peer: {_describe_exc(exc)})")
    return "\n".join(lines)


# --------------------------------------------------------------------------- ask_peer


def _interrupt_summary(interrupt: dict[str, Any] | None) -> tuple[str, str, str]:
    """-> (type, details summary, request id)."""
    interrupt = interrupt or {}
    kind = str(interrupt.get("type") or "unknown")
    request_id = str(interrupt.get("request_id") or "unknown")
    details = interrupt.get("details")
    if isinstance(details, dict) and ("permission" in details or "patterns" in details):
        patterns = details.get("patterns") or []
        if isinstance(patterns, str):
            patterns = [patterns]
        summary = str(details.get("permission") or "?")
        if patterns:
            summary += ": " + ", ".join(str(x) for x in patterns)
    elif details:
        summary = json.dumps(details, ensure_ascii=False)
    else:
        summary = "(no details)"
    return kind, summary, request_id


def _format_result(peer: str, session: str, result: AskResult, timeout_seconds: int) -> str:
    st = result.state
    if st == "TASK_STATE_COMPLETED":
        return (
            result.text
            if result.text.strip()
            else f"(peer '{peer}' session '{session}' completed with an empty reply)"
        )
    if st in ("TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"):
        kind, summary, request_id = _interrupt_summary(result.interrupt)
        what = "input" if st == "TASK_STATE_INPUT_REQUIRED" else "authorization"
        lines = [
            f"Peer '{peer}' session '{session}' is waiting for {kind} approval: {summary} (request id {request_id}).",
            f"(The peer needs {what} before it can continue.)",
            "Approve or reject it in that session's TUI, then ask again.",
        ]
        if result.text.strip():
            lines.append(f"Partial reply so far:\n{result.text}")
        return "\n".join(lines)
    if st == "TIMEOUT":
        err = result.error or f"no reply within {timeout_seconds}s"
        return _error(
            f"peer '{peer}' session '{session}' timed out: {err}\n"
            "The session may be busy with another turn or waiting for an approval in its TUI. "
            "Check that session, or ask again with a larger timeout_seconds."
        )
    if st in ("TASK_STATE_FAILED", "TASK_STATE_REJECTED", "TASK_STATE_CANCELED"):
        label = st.removeprefix("TASK_STATE_").lower()
        return _error(
            f"peer '{peer}' session '{session}' {label}: {result.error or result.text or 'no details'}"
        )
    return _error(
        f"peer '{peer}' session '{session}' ended in unexpected state {st}: {result.error or result.text or ''}".rstrip()
    )


async def _ask_opencode(
    client: A2AClient, card: AgentCard, peer: str, session: str, message: str, timeout: float
) -> AskResult:
    was_cached = state.get_session(peer, session) is not None
    ref = await resolver.resolve(client, card, peer, session, create=False)
    assert ref is not None  # opencode peers never return None
    result = await client.ask(message, opencode_session_id=ref.id, timeout=timeout)
    if was_cached and result.state == "TASK_STATE_FAILED" and result.error_type == STALE_OPENCODE_ERROR:
        log.info("cached opencode session %s for %s/%s is gone; re-resolving", ref.id, peer, session)
        resolver.invalidate(peer, session)
        ref = await resolver.resolve(client, card, peer, session, create=False)
        assert ref is not None
        result = await client.ask(message, opencode_session_id=ref.id, timeout=timeout)
    return result


async def _ask_generic(
    client: A2AClient, card: AgentCard, peer: str, session: str, message: str, timeout: float
) -> tuple[AskResult, str]:
    ref = await resolver.resolve(client, card, peer, session, create=True)
    result = await client.ask(message, context_id=ref.id if ref else None, timeout=timeout)
    note = ""
    if ref is not None and result.state == "TASK_STATE_FAILED":
        log.info("cached context %s for %s/%s failed; retrying with a fresh context", ref.id, peer, session)
        resolver.invalidate(peer, session)
        note = (
            f"Note: the saved conversation for session '{session}' failed ({result.error or 'no details'}); "
            "started a new conversation.\n"
        )
        ref = None
        result = await client.ask(message, timeout=timeout)
    if ref is None and result.context_id:
        state.set_session(peer, session, SessionRef("context", result.context_id))
    return result, note


@server.tool(
    description=(
        f"{GUARD} Sends `message` to the named `session` on A2A peer `peer` and returns the reply. "
        "Opencode peers: the session must exist with that exact title. Other peers: a new session name "
        "starts a new conversation. Replies starting with 'ERROR:' are failures."
    )
)
async def ask_peer(peer: str, session: str, message: str, timeout_seconds: int = 1200) -> str:
    try:
        p = registry.get_peer(peer)
    except RegistryError as exc:
        return _error(str(exc))
    except Exception as exc:
        return _error(f"cannot load peer registry: {_describe_exc(exc)}")
    timeout = float(max(1, timeout_seconds))
    try:
        async with client_factory(p) as client:
            try:
                card = await asyncio.wait_for(client.get_card(), min(timeout, 30.0))
            except TimeoutError:
                return _error(f"peer '{peer}' ({p.url}) is unreachable: no agent card in time")
            if card.is_opencode:
                result = await _ask_opencode(client, card, peer, session, message, timeout)
                note = ""
            else:
                result, note = await _ask_generic(client, card, peer, session, message, timeout)
            return note + _format_result(peer, session, result, timeout_seconds)
    except (SessionNotFound, AmbiguousSession) as exc:
        return _error(str(exc))
    except Exception as exc:
        if _is_a2a_error(exc):
            return _error(f"peer '{peer}': {_describe_exc(exc)}")
        log.exception("ask_peer failed")
        return _error(f"peer '{peer}': unexpected {type(exc).__name__}: {_describe_exc(exc)}")


# --------------------------------------------------------------------------- forget_session


@server.tool(description=f"{GUARD} Forgets the cached mapping of a session name on a peer.")
async def forget_session(peer: str, session: str) -> str:
    try:
        removed = resolver.invalidate(peer, session)
    except Exception as exc:
        return _error(f"cannot update session cache: {_describe_exc(exc)}")
    if removed:
        return f"Forgot session '{session}' on peer '{peer}'."
    return f"No cached session '{session}' on peer '{peer}' (nothing to forget)."


def main() -> None:
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="a2a-peers-mcp: %(levelname)s %(message)s"
    )
    # httpx logs every request at INFO; keep stderr quiet unless something is wrong.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    server.run("stdio")


if __name__ == "__main__":
    main()
