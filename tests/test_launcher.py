from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_peers import launcher, registry
from a2a_peers.launcher import LauncherError
from a2a_peers.state import SessionRef, set_session


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("A2A_PEERS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("A2A_PEERS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a, **k):  # pragma: no cover - only hit on a bug
        raise AssertionError("subprocess.Popen must not be called")

    monkeypatch.setattr(launcher.subprocess, "Popen", boom)


# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("name", ["a", "fixmonc", "A-1_b", "x" * 64])
def test_valid_names(name: str) -> None:
    assert launcher.validate_name(name) == name


@pytest.mark.parametrize("name", ["", "-a", "_a", "a b", "a/b", "x" * 65, "ä"])
def test_invalid_names(name: str) -> None:
    with pytest.raises(LauncherError):
        launcher.validate_name(name)


def test_launch_invalid_name_exits_1(tmp_path: Path, no_spawn, capsys) -> None:
    assert launcher.main(["launch", "--name", "bad name", "--workspace", str(tmp_path)]) == 1
    assert "invalid peer name" in capsys.readouterr().err


def test_launch_missing_workspace_exits_1(tmp_path: Path, no_spawn, capsys) -> None:
    rc = launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path / "nope")])
    assert rc == 1
    assert "does not exist" in capsys.readouterr().err


def test_resolve_workspace_absolute(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "ws").mkdir()
    monkeypatch.chdir(tmp_path)
    assert launcher.resolve_workspace("ws") == (tmp_path / "ws").resolve()


def test_launch_missing_binary(tmp_path: Path, no_spawn, monkeypatch, capsys) -> None:
    monkeypatch.setattr(launcher.shutil, "which", lambda _: None)
    rc = launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path)])
    assert rc == 1
    assert "opencode-a2a" in capsys.readouterr().err


def test_launch_refuses_when_running(tmp_path: Path, no_spawn, monkeypatch, capsys) -> None:
    import os

    run = launcher.run_dir()
    run.mkdir(parents=True)
    (run / "p.pid").write_text(f"{os.getpid()}\n")  # our own pid: definitely alive
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
    rc = launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path)])
    assert rc == 1
    assert "already has a running" in capsys.readouterr().err


def test_same_ports_rejected() -> None:
    with pytest.raises(LauncherError):
        launcher.pick_ports(5000, 5000)


def test_invalid_port_rejected() -> None:
    with pytest.raises(LauncherError):
        launcher.pick_ports(0, None)


# ---------------------------------------------------------------- ports


def test_free_ports_distinct() -> None:
    for _ in range(20):
        p, q = launcher.pick_ports(None, None)
        assert p != q
        assert 0 < p < 65536 and 0 < q < 65536


def test_given_ports_kept() -> None:
    assert launcher.pick_ports(4101, 8101) == (4101, 8101)
    p, q = launcher.pick_ports(4101, None)
    assert p == 4101 and q != 4101


# ---------------------------------------------------------------- env


def test_build_a2a_env(tmp_path: Path) -> None:
    tricky = 'tok"en\\with\nstuff'
    env = launcher.build_a2a_env(
        name="fixmonc",
        token=tricky,
        workspace=tmp_path / "ws",
        opencode_port=4101,
        a2a_port=8101,
        run_directory=tmp_path / "run",
        base_env={"PATH": "/bin", "A2A_PORT": "1"},
    )
    assert json.loads(env["A2A_STATIC_AUTH_CREDENTIALS"]) == [
        {"scheme": "bearer", "token": tricky, "principal": "fixmonc"}
    ]
    assert env["OPENCODE_BASE_URL"] == "http://127.0.0.1:4101"
    assert env["A2A_TASK_STORE_DATABASE_URL"] == f"sqlite+aiosqlite:///{tmp_path / 'run' / 'fixmonc.db'}"
    assert env["A2A_HOST"] == "127.0.0.1"
    assert env["A2A_PORT"] == "8101"
    assert env["A2A_PUBLIC_URL"] == "http://127.0.0.1:8101"
    assert env["OPENCODE_WORKSPACE_ROOT"] == str(tmp_path / "ws")
    assert env["PATH"] == "/bin"


# ---------------------------------------------------------------- launch (mocked)


class FakeProc:
    pid = 424242
    returncode = None

    def poll(self):
        return None


class FakeResp:
    def __init__(self, status: int, payload: object | None = None) -> None:
        self.status_code = status
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise launcher.httpx.HTTPStatusError("err", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> object:
        return self._payload

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300


def _stub_launch_http(monkeypatch: pytest.MonkeyPatch, *, sessions: list | None = None) -> None:
    def fake_get(url, **kwargs):
        if str(url).endswith("/session"):
            return FakeResp(200, sessions if sessions is not None else [])
        return FakeResp(200)

    def fake_post(url, **kwargs):
        return FakeResp(200, {"id": "ses_new"})

    monkeypatch.setattr(launcher.httpx, "get", fake_get)
    monkeypatch.setattr(launcher.httpx, "post", fake_post)
    monkeypatch.setattr(launcher, "port_listening", lambda *a, **k: True)
    monkeypatch.setattr(launcher, "wait_for_opencode", lambda *a, **k: True)
    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_launch_happy_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    calls: dict = {}
    pops: list[list[str]] = []

    def fake_popen(cmd, **kwargs):
        pops.append(list(cmd))
        if list(cmd)[:2] == ["/usr/bin/opencode-a2a", "serve"]:
            calls["cmd"] = cmd
            calls["kwargs"] = kwargs
        return FakeProc()

    _stub_launch_http(monkeypatch, sessions=[])
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(launcher, "port_listening", lambda *a, **k: False)

    rc = launcher.main(
        ["launch", "--name", "p", "--workspace", str(ws), "--opencode-port", "4101", "--a2a-port", "8101"]
    )
    assert rc == 0
    assert calls["cmd"] == ["/usr/bin/opencode-a2a", "serve"]
    assert ["/usr/bin/opencode", "--port", "4101"] in pops
    kw = calls["kwargs"]
    assert kw["start_new_session"] is True
    token = json.loads(kw["env"]["A2A_STATIC_AUTH_CREDENTIALS"])[0]["token"]
    assert len(token) == 48

    peer = registry.get_peer("p")
    assert peer.url == "http://127.0.0.1:8101"
    assert peer.token == token
    assert peer.opencode_port == 4101
    assert peer.workspace == str(ws.resolve())
    assert (launcher.run_dir() / "p.pid").read_text().strip() == "424242"
    assert (launcher.run_dir() / "p.opencode.pid").read_text().strip() == "424242"
    out = capsys.readouterr().out
    assert "Session title 'p' is ready" in out
    from a2a_peers import state
    from a2a_peers.state import SessionRef

    assert state.get_session("p", "p") == SessionRef("opencode_session", "ses_new")


def test_launch_force_stops_previous_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run = launcher.run_dir()
    run.mkdir(parents=True)
    (run / "p.pid").write_text("111\n")
    alive = {111: True}
    signalled: list[tuple[int, int]] = []

    def fake_killpg(pid: int, sig: int) -> None:
        signalled.append((pid, sig))
        alive[pid] = False

    monkeypatch.setattr(launcher, "pid_alive", lambda pid: alive.get(pid, False))
    monkeypatch.setattr(launcher, "_is_group_leader", lambda pid: True)
    monkeypatch.setattr(launcher.os, "killpg", fake_killpg)
    monkeypatch.setattr(launcher.subprocess, "Popen", lambda cmd, **kw: FakeProc())
    _stub_launch_http(monkeypatch)

    assert launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path), "--force"]) == 0
    assert signalled and signalled[0][0] == 111
    assert (run / "p.pid").read_text().strip() == str(FakeProc.pid)


def test_launch_reuses_registry_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    registry.upsert_peer(registry.Peer(name="p", url="http://127.0.0.1:1", token="existing"))
    seen: dict = {}

    def fake_popen(cmd, **kwargs):
        seen["env"] = kwargs["env"]
        return FakeProc()

    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    _stub_launch_http(monkeypatch)

    assert launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path)]) == 0
    assert json.loads(seen["env"]["A2A_STATIC_AUTH_CREDENTIALS"])[0]["token"] == "existing"
    assert registry.get_peer("p").token == "existing"
    assert "Reusing" in capsys.readouterr().out


def test_launch_process_died(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    class DeadProc(FakeProc):
        returncode = 3

        def poll(self):
            return 3

    def fake_popen(cmd, **kwargs):
        kwargs["stdout"].write(b"boom: port in use\n")
        return DeadProc()

    monkeypatch.setattr(launcher.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(launcher.subprocess, "Popen", fake_popen)
    assert launcher.main(["launch", "--name", "p", "--workspace", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "boom: port in use" in err
    assert not (launcher.run_dir() / "p.pid").exists()


# ---------------------------------------------------------------- add / kill / list


def test_add_writes_registry(capsys) -> None:
    assert launcher.main(["add", "ext", "--url", "https://agent.example/a2a/", "--token", "t"]) == 0
    peer = registry.get_peer("ext")
    assert peer.url == "https://agent.example/a2a"
    assert peer.token == "t"
    assert peer.workspace is None and peer.opencode_port is None


def test_add_without_token() -> None:
    assert launcher.main(["add", "ext", "--url", "http://127.0.0.1:9000"]) == 0
    assert registry.get_peer("ext").token is None


def test_add_bad_url(capsys) -> None:
    assert launcher.main(["add", "ext", "--url", "ftp://x"]) == 1
    assert "invalid --url" in capsys.readouterr().err


def test_kill_without_pid_file(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    def no_kill(*a):  # pragma: no cover
        raise AssertionError("killpg must not be called")

    monkeypatch.setattr(launcher.os, "killpg", no_kill)
    assert launcher.main(["kill", "nobody"]) == 0
    assert "no pid file" in capsys.readouterr().out


def test_kill_forget_removes_registry() -> None:
    launcher.main(["add", "ext", "--url", "http://127.0.0.1:9000"])
    assert launcher.main(["kill", "ext", "--forget"]) == 0
    assert "ext" not in registry.load_peers()


def test_kill_stale_pid_file(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run = launcher.run_dir()
    run.mkdir(parents=True)
    (run / "p.pid").write_text("999999\n")
    monkeypatch.setattr(launcher, "pid_alive", lambda pid: False)
    assert launcher.main(["kill", "p"]) == 0
    assert not (run / "p.pid").exists()
    assert "stale" in capsys.readouterr().out


def test_list_empty(capsys) -> None:
    assert launcher.main(["list"]) == 0
    assert "No peers" in capsys.readouterr().out


def test_list_external(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    launcher.main(["add", "ext", "--url", "http://127.0.0.1:9000"])
    capsys.readouterr()
    assert launcher.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "ext" in out and "external" in out
    assert "SESSION" in out


def test_list_shows_cached_session(capsys) -> None:
    launcher.main(["add", "ext", "--url", "http://127.0.0.1:9000"])
    set_session("ext", "ext", SessionRef("opencode_session", "ses_abc123"))
    capsys.readouterr()
    assert launcher.main(["list"]) == 0
    out = capsys.readouterr().out
    assert "ses_abc123" in out


def test_check_unknown_peer(capsys) -> None:
    assert launcher.main(["check", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


def test_kill_refuses_recycled_pid(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run = launcher.run_dir()
    run.mkdir(parents=True)
    (run / "p.pid").write_text("4242\n")
    monkeypatch.setattr(launcher, "pid_alive", lambda pid: True)
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: 1)  # not its own group leader

    def no_kill(*a):  # pragma: no cover
        raise AssertionError("killpg must not be called")

    monkeypatch.setattr(launcher.os, "killpg", no_kill)
    assert launcher.main(["kill", "p"]) == 0
    assert "not a process-group leader" in capsys.readouterr().err
    assert not (run / "p.pid").exists()


def test_kill_running_group(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    run = launcher.run_dir()
    run.mkdir(parents=True)
    (run / "p.pid").write_text("4242\n")
    alive = {"v": True}
    sent: list = []
    monkeypatch.setattr(launcher, "pid_alive", lambda pid: alive["v"])
    monkeypatch.setattr(launcher.os, "getpgid", lambda pid: pid)

    def fake_killpg(pgid, sig):
        sent.append((pgid, sig))
        alive["v"] = False

    monkeypatch.setattr(launcher.os, "killpg", fake_killpg)
    assert launcher.main(["kill", "p"]) == 0
    assert sent == [(4242, launcher.signal.SIGTERM)]
    assert "Stopped" in capsys.readouterr().out


def test_tui_command_quotes_workspace() -> None:
    assert launcher._tui_command("/tmp/my ws", 4101) == "cd '/tmp/my ws' && opencode --port 4101"


def test_ensure_titled_session_reuses_existing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        launcher.httpx,
        "get",
        lambda *a, **k: FakeResp(200, [{"id": "ses_old", "title": "p"}]),
    )

    def no_post(*a, **k):  # pragma: no cover
        raise AssertionError("POST /session must not be called")

    monkeypatch.setattr(launcher.httpx, "post", no_post)
    assert launcher.ensure_titled_session(4101, "p", "/ws") == "ses_old"


def test_ensure_titled_session_creates(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(launcher.httpx, "get", lambda *a, **k: FakeResp(200, []))
    monkeypatch.setattr(launcher.httpx, "post", lambda *a, **k: FakeResp(200, {"id": "ses_new"}))
    assert launcher.ensure_titled_session(4101, "p", "/ws") == "ses_new"
