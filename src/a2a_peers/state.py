"""Session-name cache stored in state.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, get_args

from a2a_peers.jsonfile import locked, warn, write_json_atomic
from a2a_peers.paths import state_file

SessionKind = Literal["opencode_session", "context"]
_VALID_KINDS: tuple[str, ...] = get_args(SessionKind)


@dataclass(frozen=True)
class SessionRef:
    kind: SessionKind
    id: str

    def __post_init__(self) -> None:
        if self.kind not in _VALID_KINDS:
            raise ValueError(f"invalid session kind {self.kind!r}; expected one of {', '.join(_VALID_KINDS)}")
        if not isinstance(self.id, str) or not self.id:
            raise ValueError("session id must be a non-empty string")


def _read() -> dict[str, Any]:
    path = state_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"sessions": {}}
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        warn(f"ignoring corrupt state file {path}: {exc}")
        return {"sessions": {}}
    if not isinstance(data, dict) or not isinstance(data.get("sessions", {}), dict):
        warn(f"ignoring malformed state file {path}")
        return {"sessions": {}}
    data.setdefault("sessions", {})
    return data


def _write(data: dict[str, Any]) -> None:
    write_json_atomic(state_file(), data)


def _ref_from_entry(entry: Any) -> SessionRef | None:
    if not isinstance(entry, dict):
        return None
    try:
        return SessionRef(kind=entry.get("kind"), id=entry.get("id"))  # type: ignore[arg-type]
    except ValueError:
        return None


def _peer_sessions(data: dict[str, Any], peer: str) -> dict[str, Any]:
    sessions = data["sessions"].get(peer)
    return sessions if isinstance(sessions, dict) else {}


def get_session(peer: str, name: str) -> SessionRef | None:
    return _ref_from_entry(_peer_sessions(_read(), peer).get(name))


def set_session(peer: str, name: str, ref: SessionRef) -> None:
    with locked(state_file()):
        data = _read()
        sessions = data["sessions"].get(peer)
        if not isinstance(sessions, dict):
            sessions = {}
            data["sessions"][peer] = sessions
        sessions[name] = {"kind": ref.kind, "id": ref.id}
        _write(data)


def forget_session(peer: str, name: str) -> bool:
    with locked(state_file()):
        data = _read()
        sessions = _peer_sessions(data, peer)
        if name not in sessions:
            return False
        del sessions[name]
        if not sessions:
            data["sessions"].pop(peer, None)
        _write(data)
        return True


def list_cached(peer: str) -> dict[str, SessionRef]:
    result: dict[str, SessionRef] = {}
    for name, entry in _peer_sessions(_read(), peer).items():
        ref = _ref_from_entry(entry)
        if ref is not None:
            result[name] = ref
    return result


def forget_peer(peer: str) -> None:
    with locked(state_file()):
        data = _read()
        if peer in data["sessions"]:
            del data["sessions"][peer]
            _write(data)
