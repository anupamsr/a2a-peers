"""`a2a-peers` CLI: launch / list / kill / check opencode-a2a bridges and register peers."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx

from a2a_peers import registry, state
from a2a_peers.a2a_client import CARD_PATH, OPENCODE_SESSION_EXT, is_loopback_url
from a2a_peers.paths import run_dir
from a2a_peers.registry import Peer, RegistryError
from a2a_peers.state import SessionRef

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
OPENCODE_A2A_BIN = "opencode-a2a"
OPENCODE_BIN = "opencode"
STARTUP_TIMEOUT = 10.0
KILL_GRACE = 5.0
LOG_TAIL_LINES = 30
OPENCODE_WAIT = 120.0


class LauncherError(Exception):
    """User-facing error; message is printed to stderr and the CLI exits 1."""


# --------------------------------------------------------------------------- helpers


def validate_name(name: str) -> str:
    if not NAME_RE.match(name):
        raise LauncherError(
            f"invalid peer name {name!r}: must match {NAME_RE.pattern} "
            "(letters, digits, '_' and '-', max 64 chars, not starting with '_' or '-')"
        )
    return name


def validate_port(port: int | None, what: str) -> int | None:
    if port is not None and not (1 <= port <= 65535):
        raise LauncherError(f"invalid {what} {port}: must be in 1..65535")
    return port


def resolve_workspace(workspace: str) -> Path:
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise LauncherError(f"workspace {workspace!r} does not exist or is not a directory")
    return path


def _free_port(exclude: set[int]) -> int:
    for _ in range(50):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        if port not in exclude:
            return port
    raise LauncherError("could not find a free TCP port on 127.0.0.1")


def pick_ports(opencode_port: int | None, a2a_port: int | None) -> tuple[int, int]:
    """Return (opencode_port, a2a_port), choosing free ports for any not given. Never equal."""
    validate_port(opencode_port, "--opencode-port")
    validate_port(a2a_port, "--a2a-port")
    if opencode_port is not None and a2a_port is not None and opencode_port == a2a_port:
        raise LauncherError(f"--opencode-port and --a2a-port must differ (both {opencode_port})")
    if opencode_port is None:
        opencode_port = _free_port({a2a_port} if a2a_port is not None else set())
    if a2a_port is None:
        a2a_port = _free_port({opencode_port})
    return opencode_port, a2a_port


def build_a2a_env(
    *,
    name: str,
    token: str,
    workspace: Path | str,
    opencode_port: int,
    a2a_port: int,
    run_directory: Path,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Environment for `opencode-a2a serve` (pure function; see CONTRACTS.md)."""
    env = dict(os.environ if base_env is None else base_env)
    creds = [{"scheme": "bearer", "token": token, "principal": name}]
    env.update(
        {
            "A2A_STATIC_AUTH_CREDENTIALS": json.dumps(creds),
            "OPENCODE_BASE_URL": f"http://127.0.0.1:{opencode_port}",
            "A2A_TASK_STORE_DATABASE_URL": f"sqlite+aiosqlite:///{run_directory / f'{name}.db'}",
            "A2A_HOST": "127.0.0.1",
            "A2A_PORT": str(a2a_port),
            "A2A_PUBLIC_URL": f"http://127.0.0.1:{a2a_port}",
            "OPENCODE_WORKSPACE_ROOT": str(workspace),
        }
    )
    return env


def pid_file(name: str) -> Path:
    return run_dir() / f"{name}.pid"


def log_file(name: str) -> Path:
    return run_dir() / f"{name}.log"


def read_pid(name: str) -> int | None:
    path = pid_file(name)
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def log_tail(path: Path, lines: int = LOG_TAIL_LINES) -> str:
    try:
        content = path.read_text(errors="replace").splitlines()
    except FileNotFoundError:
        return "(no log file)"
    return "\n".join(content[-lines:]) or "(log is empty)"


def _tui_command(workspace: Path | str, opencode_port: int) -> str:
    return f"cd {shlex.quote(str(workspace))} && {OPENCODE_BIN} --port {opencode_port}"


def opencode_pid_file(name: str) -> Path:
    return run_dir() / f"{name}.opencode.pid"


def read_opencode_pid(name: str) -> int | None:
    path = opencode_pid_file(name)
    try:
        return int(path.read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def _require_binary(name: str, install_hint: str) -> str:
    binary = shutil.which(name)
    if binary is None:
        raise LauncherError(f"`{name}` not found on PATH; {install_hint}")
    return binary


def wait_for_opencode(port: int, timeout: float = OPENCODE_WAIT) -> bool:
    """True if opencode's HTTP API answers on `port` before `timeout` seconds."""
    health = f"http://127.0.0.1:{port}/global/health"
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(health, timeout=1.0, trust_env=False).is_success:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def ensure_titled_session(opencode_port: int, title: str, workspace: Path | str) -> str:
    """Create an opencode session named `title` if none exists. Returns the session id."""
    base = f"http://127.0.0.1:{opencode_port}"
    params = {"directory": str(workspace)}
    try:
        resp = httpx.get(f"{base}/session", params=params, timeout=5.0, trust_env=False)
        resp.raise_for_status()
        sessions = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LauncherError(f"cannot list opencode sessions on port {opencode_port}: {exc}") from exc
    if not isinstance(sessions, list):
        raise LauncherError(f"unexpected /session response from opencode on port {opencode_port}")
    for item in sessions:
        if isinstance(item, dict) and item.get("title") == title and isinstance(item.get("id"), str):
            return item["id"]
    try:
        resp = httpx.post(
            f"{base}/session",
            params=params,
            json={"title": title},
            timeout=5.0,
            trust_env=False,
        )
        resp.raise_for_status()
        created = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise LauncherError(f"cannot create opencode session {title!r}: {exc}") from exc
    sid = created.get("id") if isinstance(created, dict) else None
    if not isinstance(sid, str) or not sid:
        raise LauncherError(f"opencode created session {title!r} but returned no id")
    return sid


def start_opencode(name: str, workspace: Path, opencode_port: int) -> subprocess.Popen[bytes]:
    binary = _require_binary(
        OPENCODE_BIN,
        "install opencode and make sure its bin directory is on PATH",
    )
    existing = read_opencode_pid(name)
    if existing is not None and pid_alive(existing):
        raise LauncherError(
            f"peer {name!r} already has a running opencode (pid {existing}); "
            f"use `a2a-peers kill {name}` first or pass --force"
        )
    log_path = log_file(name)
    with open(log_path, "ab") as log_fh:
        log_fh.write(f"\n--- starting {OPENCODE_BIN} --port {opencode_port} ---\n".encode())
        proc = subprocess.Popen(
            [binary, "--port", str(opencode_port)],
            cwd=str(workspace),
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    opencode_pid_file(name).write_text(f"{proc.pid}\n")
    return proc


def _is_group_leader(pid: int) -> bool:
    """True if `pid` still leads its own process group (as our start_new_session child does).
    Guards killpg against a recycled pid that now belongs to an unrelated process."""
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError):
        return False


def _err(message: str) -> None:
    print(f"a2a-peers: error: {message}", file=sys.stderr)


def _warn(message: str) -> None:
    print(f"a2a-peers: warning: {message}", file=sys.stderr)


# --------------------------------------------------------------------------- commands


def cmd_launch(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    workspace = resolve_workspace(args.workspace)

    existing_pid = read_pid(name)
    existing_oc = read_opencode_pid(name)
    running = (existing_pid is not None and pid_alive(existing_pid)) or (
        existing_oc is not None and pid_alive(existing_oc)
    )
    if running:
        if not args.force:
            raise LauncherError(
                f"peer {name!r} already has a running process; "
                f"use `a2a-peers kill {name}` first or pass --force"
            )
        _warn(f"--force: stopping the previous processes for {name!r} first")
        cmd_kill(argparse.Namespace(name=name, forget=False))

    opencode_port, a2a_port = pick_ports(args.opencode_port, args.a2a_port)

    binary = _require_binary(
        OPENCODE_A2A_BIN,
        "install it (e.g. `uv tool install --force --editable /path/to/opencode-a2a`) "
        "and make sure its bin directory is on PATH",
    )
    _require_binary(OPENCODE_BIN, "install opencode and make sure its bin directory is on PATH")

    previous = registry.load_peers().get(name)
    if previous is not None and previous.token:
        token = previous.token
        print(f"Reusing existing registry token for {name!r} (configured clients keep working).")
    else:
        token = secrets.token_hex(24)
        print(f"Generated a new bearer token for {name!r}.")

    rdir = run_dir()
    rdir.mkdir(parents=True, exist_ok=True, mode=0o700)
    env = build_a2a_env(
        name=name,
        token=token,
        workspace=workspace,
        opencode_port=opencode_port,
        a2a_port=a2a_port,
        run_directory=rdir,
    )
    log_path = log_file(name)
    with open(log_path, "ab") as log_fh:
        proc = subprocess.Popen(
            [binary, "serve"],
            cwd=str(workspace),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    pid_file(name).write_text(f"{proc.pid}\n")

    url = f"http://127.0.0.1:{a2a_port}"
    registry.upsert_peer(
        Peer(name=name, url=url, token=token, workspace=str(workspace), opencode_port=opencode_port)
    )

    ready = False
    deadline = time.monotonic() + STARTUP_TIMEOUT
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            _err(f"opencode-a2a exited with code {proc.returncode}; last lines of {log_path}:")
            print(log_tail(log_path), file=sys.stderr)
            pid_file(name).unlink(missing_ok=True)
            return 1
        try:
            if httpx.get(url + CARD_PATH, timeout=1.0, trust_env=not is_loopback_url(url)).status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.3)

    print(f"Started opencode-a2a for {name!r} (pid {proc.pid}), log: {log_path}")
    if ready:
        print(f"A2A endpoint is up: {url}")
    else:
        _warn(f"A2A endpoint {url} did not answer within {STARTUP_TIMEOUT:.0f}s; check {log_path}")

    if port_listening(opencode_port):
        print(f"Reusing opencode already listening on port {opencode_port}.")
    else:
        oc = start_opencode(name, workspace, opencode_port)
        print(f"Started opencode for {name!r} (pid {oc.pid}) on port {opencode_port}.")

    if not wait_for_opencode(opencode_port):
        _warn(
            f"opencode on port {opencode_port} did not answer within {OPENCODE_WAIT:.0f}s; "
            f"check {log_path}. Session {name!r} was not created."
        )
        print(f"Start it yourself with: {_tui_command(workspace, opencode_port)}")
        return 0

    session_id = ensure_titled_session(opencode_port, name, workspace)
    state.set_session(name, name, SessionRef("opencode_session", session_id))
    print(f"Session title {name!r} is ready (id {session_id}). Ask this peer with session={name!r}.")
    return 0


def _process_status(name: str) -> str:
    pid = read_pid(name)
    if pid is None:
        return "external"
    return "alive" if pid_alive(pid) else "dead"


def _cached_session_id(name: str) -> str | None:
    try:
        cached = state.list_cached(name)
    except Exception:
        return None
    ref = cached.get(name)
    if ref is not None:
        return ref.id
    if len(cached) == 1:
        return next(iter(cached.values())).id
    return None


def _attach_command(peer: Peer) -> str:
    if peer.opencode_port is None:
        return "-"
    parts = [OPENCODE_BIN, "attach", f"http://127.0.0.1:{peer.opencode_port}"]
    if peer.workspace:
        parts += ["--dir", shlex.quote(peer.workspace)]
    sid = _cached_session_id(peer.name)
    if sid:
        parts += ["-s", sid]
    return " ".join(parts)


def cmd_list(args: argparse.Namespace) -> int:
    peers = registry.load_peers()
    if not peers:
        print("No peers registered.")
        return 0
    rows: list[list[str]] = [["NAME", "A2A URL", "OC PORT", "OC LISTENING", "A2A PROC", "ATTACH"]]
    for peer in peers.values():
        if peer.opencode_port is not None:
            listening = "yes" if port_listening(peer.opencode_port) else "no"
        else:
            listening = "-"
        rows.append(
            [
                peer.name,
                peer.url,
                str(peer.opencode_port) if peer.opencode_port is not None else "-",
                listening,
                _process_status(peer.name),
                _attach_command(peer),
            ]
        )
    widths = [max(len(r[i]) for r in rows) for i in range(len(rows[0]))]
    for row in rows:
        print("  ".join(cell.ljust(w) for cell, w in zip(row, widths, strict=True)).rstrip())
    return 0


def _stop_pid(name: str, pid: int, pfile: Path, label: str) -> None:
    if not pid_alive(pid):
        pfile.unlink(missing_ok=True)
        print(f"{label} for {name!r} (pid {pid}) is not running; removed stale pid file.")
        return
    if not _is_group_leader(pid):
        pfile.unlink(missing_ok=True)
        _warn(
            f"pid {pid} from {pfile} is alive but is not a process-group leader (pid reused?); "
            "not signalling it. Removed the pid file."
        )
        return
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + KILL_GRACE
    while time.monotonic() < deadline and pid_alive(pid):
        time.sleep(0.1)
    if pid_alive(pid):
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        print(f"Sent SIGKILL to {label} for {name!r} (pid {pid}).")
    else:
        print(f"Stopped {label} for {name!r} (pid {pid}).")
    pfile.unlink(missing_ok=True)


def cmd_kill(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    oc_pid = read_opencode_pid(name)
    oc_file = opencode_pid_file(name)
    if oc_pid is None:
        if oc_file.exists():
            _warn(f"pid file {oc_file} is unreadable; removing it")
            oc_file.unlink(missing_ok=True)
    else:
        _stop_pid(name, oc_pid, oc_file, "opencode")

    pid = read_pid(name)
    pfile = pid_file(name)
    if pid is None:
        if pfile.exists():
            _warn(f"pid file {pfile} is unreadable; removing it")
            pfile.unlink(missing_ok=True)
        extra = ""
        if not args.forget and name in registry.load_peers():
            extra = f" It is still in the registry (`a2a-peers kill {name} --forget` to remove it)."
        print(f"No launched opencode-a2a process for {name!r} (no pid file).{extra}")
    else:
        _stop_pid(name, pid, pfile, "opencode-a2a")

    if args.forget:
        if registry.remove_peer(name):
            print(f"Removed {name!r} from the registry.")
        else:
            print(f"{name!r} was not in the registry.")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    peer = registry.get_peer(args.name)
    ok = True
    print(f"Peer {peer.name}: {peer.url}")
    try:
        resp = httpx.get(peer.url + CARD_PATH, timeout=5.0, trust_env=not is_loopback_url(peer.url))
        resp.raise_for_status()
        card = resp.json()
        if not isinstance(card, dict):
            raise ValueError("agent card is not a JSON object")
        caps = card.get("capabilities")
        caps = caps if isinstance(caps, dict) else {}
        uris = [e.get("uri") for e in caps.get("extensions") or [] if isinstance(e, dict)]
        print(f"  agent card: OK (name={card.get('name')!r})")
        print(f"  streaming: {bool(caps.get('streaming'))}")
        print(f"  opencode peer: {OPENCODE_SESSION_EXT in uris}")
    except (httpx.HTTPError, ValueError) as exc:
        ok = False
        print(f"  agent card: FAILED ({exc})")
        _err(f"cannot fetch agent card from {peer.url}{CARD_PATH}")

    if ok:
        try:
            resp = httpx.post(
                peer.url + "/message:send", json={}, timeout=5.0, trust_env=not is_loopback_url(peer.url)
            )
            if resp.status_code == 401:
                print("  unauthenticated request rejected: OK (401)")
            else:
                print(f"  unauthenticated request: got HTTP {resp.status_code}")
                _warn(f"expected 401 for unauthenticated POST /message:send, got {resp.status_code}")
        except httpx.HTTPError as exc:
            _warn(f"auth probe failed: {exc}")

    if peer.opencode_port is not None:
        health = f"http://127.0.0.1:{peer.opencode_port}/global/health"
        try:
            resp = httpx.get(health, timeout=3.0, trust_env=False)
            print(f"  opencode on port {peer.opencode_port}: reachable (HTTP {resp.status_code})")
        except httpx.HTTPError:
            print(f"  opencode on port {peer.opencode_port}: NOT running")
            if peer.workspace:
                print(f"    start it with: {_tui_command(peer.workspace, peer.opencode_port)}")
    return 0 if ok else 1


def cmd_add(args: argparse.Namespace) -> int:
    name = validate_name(args.name)
    url = args.url.rstrip("/")
    if not re.match(r"^https?://[^/\s]+", url):
        raise LauncherError(f"invalid --url {args.url!r}: must start with http:// or https://")
    registry.upsert_peer(Peer(name=name, url=url, token=args.token or None))
    print(f"Registered peer {name!r} -> {url} ({'with' if args.token else 'without'} token).")
    return 0


# --------------------------------------------------------------------------- CLI


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="a2a-peers", description="Manage A2A peers (opencode-a2a bridges and external agents)."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser(
        "launch", help="start opencode-a2a and opencode for a workspace, and create a titled session"
    )
    p.add_argument("--name", required=True)
    p.add_argument("--workspace", required=True)
    p.add_argument("--opencode-port", type=int)
    p.add_argument("--a2a-port", type=int)
    p.add_argument("--force", action="store_true", help="launch even if one is already running")
    p.set_defaults(func=cmd_launch)

    p = sub.add_parser("list", help="list registered peers and their status")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("kill", help="stop a launched opencode-a2a process")
    p.add_argument("name")
    p.add_argument("--forget", action="store_true", help="also remove it from the registry")
    p.set_defaults(func=cmd_kill)

    p = sub.add_parser("check", help="health-check a peer")
    p.add_argument("name")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("add", help="register an external A2A peer (no process management)")
    p.add_argument("name")
    p.add_argument("--url", required=True)
    p.add_argument("--token")
    p.set_defaults(func=cmd_add)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return int(args.func(args))
    except (LauncherError, RegistryError, OSError) as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
