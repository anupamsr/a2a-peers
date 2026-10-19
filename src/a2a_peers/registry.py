"""Peer registry stored in peers.json (mode 0600)."""

from __future__ import annotations

import json
import stat
from dataclasses import dataclass
from typing import Any

from a2a_peers.jsonfile import locked, warn, write_json_atomic
from a2a_peers.paths import registry_file


@dataclass(frozen=True)
class Peer:
    name: str
    url: str  # A2A base URL, no trailing slash
    token: str | None  # bearer token; None = no auth
    workspace: str | None = None
    opencode_port: int | None = None


class RegistryError(Exception):
    pass


def _normalize_url(url: str) -> str:
    return url.rstrip("/")


def _read_raw() -> dict[str, Any]:
    path = registry_file()
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"peers": {}}
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except FileNotFoundError:
        mode = 0o600
    if mode & 0o077:
        warn(f"{path} has mode {oct(mode)}; it contains tokens and should be 0600 (chmod 600 {path})")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RegistryError(f"invalid JSON in {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RegistryError(f"invalid registry {path}: top-level value must be an object")
    peers = data.setdefault("peers", {})
    if not isinstance(peers, dict):
        raise RegistryError(f"invalid registry {path}: 'peers' must be an object")
    return data


def _peer_from_entry(name: str, entry: Any) -> Peer:
    path = registry_file()
    if not isinstance(entry, dict) or not isinstance(entry.get("url"), str) or not entry["url"]:
        raise RegistryError(f"invalid entry for peer '{name}' in {path}: missing 'url'")
    port = entry.get("opencode_port")
    if port is not None and (isinstance(port, bool) or not isinstance(port, int)):
        raise RegistryError(f"invalid entry for peer '{name}' in {path}: 'opencode_port' must be an integer")
    return Peer(
        name=name,
        url=_normalize_url(entry["url"]),
        token=entry.get("token") or None,
        workspace=entry.get("workspace"),
        opencode_port=int(port) if port is not None else None,
    )


def _peer_to_entry(peer: Peer) -> dict[str, Any]:
    entry: dict[str, Any] = {"url": _normalize_url(peer.url), "token": peer.token}
    if peer.workspace is not None:
        entry["workspace"] = peer.workspace
    if peer.opencode_port is not None:
        entry["opencode_port"] = peer.opencode_port
    return entry


def load_peers() -> dict[str, Peer]:
    data = _read_raw()
    return {name: _peer_from_entry(name, entry) for name, entry in data["peers"].items()}


def get_peer(name: str) -> Peer:
    peers = load_peers()
    try:
        return peers[name]
    except KeyError:
        known = ", ".join(peers) if peers else "(none)"
        raise RegistryError(f"unknown peer '{name}'; known: {known}") from None


def upsert_peer(peer: Peer) -> None:
    with locked(registry_file()):
        data = _read_raw()
        data["peers"][peer.name] = _peer_to_entry(peer)
        write_json_atomic(registry_file(), data)


def remove_peer(name: str) -> bool:
    if not registry_file().exists():
        return False
    with locked(registry_file()):
        data = _read_raw()
        if name not in data["peers"]:
            return False
        del data["peers"][name]
        write_json_atomic(registry_file(), data)
        return True
