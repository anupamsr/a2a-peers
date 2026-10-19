"""Resolve a human session name to a SessionRef (uses an A2A client + the state cache)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from a2a_peers import state
from a2a_peers.state import SessionRef

if TYPE_CHECKING:
    from a2a_peers.a2a_client import A2AClient, AgentCard

SEARCH_LIMIT = 50
LISTING_LIMIT = 20


class SessionNotFound(Exception):
    """No session with that name; the message lists known names / candidate titles."""


class AmbiguousSession(Exception):
    """Several sessions match the name; the message lists the matching ids."""


def _title(item: dict[str, Any]) -> str | None:
    title = item.get("title")
    return title.strip() if isinstance(title, str) and title.strip() else None


def _describe(item: dict[str, Any]) -> str:
    title = _title(item)
    return f"{item.get('id')} ({title!r})" if title else f"{item.get('id')} (untitled)"


async def _not_found_opencode(client: A2AClient, peer: str, name: str) -> SessionNotFound:
    try:
        items = await client.list_opencode_sessions(limit=LISTING_LIMIT)
    except Exception as exc:  # listing is best-effort; still report the miss
        available = f"(could not list sessions: {exc})"
    else:
        titles = [t for t in (_title(i) for i in items) if t]
        available = ", ".join(repr(t) for t in titles) if titles else "(no titled sessions)"
    return SessionNotFound(
        f"no session titled {name!r} on peer '{peer}'. Available titled sessions: {available}. "
        f"Create/rename a session with that exact title in the peer's TUI, then ask again."
    )


async def _resolve_opencode(client: A2AClient, peer: str, name: str) -> SessionRef:
    wanted = name.strip()
    hits = await client.list_opencode_sessions(search=wanted, limit=SEARCH_LIMIT)
    if hits and all(_title(h) is None for h in hits):
        # Older plugin without titles: `search` is a case-insensitive substring filter.
        if len(hits) > 1:
            ids = ", ".join(str(h.get("id")) for h in hits)
            raise AmbiguousSession(
                f"{len(hits)} sessions on peer '{peer}' match {name!r} and the peer does not report titles; "
                f"matching ids: {ids}. Give the session a more specific title in the peer's TUI."
            )
        matches = hits
    else:
        matches = [h for h in hits if _title(h) == wanted]
    if len(matches) > 1:
        ids = ", ".join(str(m.get("id")) for m in matches)
        raise AmbiguousSession(
            f"{len(matches)} sessions on peer '{peer}' are titled {name!r}; matching ids: {ids}. "
            f"Rename all but one in the peer's TUI."
        )
    if not matches:
        raise await _not_found_opencode(client, peer, name)
    ref = SessionRef("opencode_session", str(matches[0]["id"]))
    state.set_session(peer, name, ref)
    return ref


async def resolve(
    client: A2AClient, card: AgentCard, peer: str, name: str, *, create: bool
) -> SessionRef | None:
    """Return the SessionRef for `name` on `peer`.

    Returns None only for generic peers with `create=True`: the caller sends without a
    contextId and caches the contextId the peer returns.
    """
    cached = state.get_session(peer, name)
    if cached is not None:
        return cached
    if card.is_opencode:
        return await _resolve_opencode(client, peer, name)
    if create:
        return None
    known = ", ".join(repr(n) for n in state.list_cached(peer)) or "(none)"
    raise SessionNotFound(f"unknown session {name!r} on peer '{peer}'; known sessions: {known}")


def invalidate(peer: str, name: str) -> bool:
    return state.forget_session(peer, name)


__all__ = ["AmbiguousSession", "SessionNotFound", "invalidate", "resolve"]
