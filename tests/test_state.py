from __future__ import annotations

import json
from pathlib import Path

import pytest

from a2a_peers import paths
from a2a_peers.state import SessionRef, forget_peer, forget_session, get_session, list_cached, set_session


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("A2A_PEERS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("A2A_PEERS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_missing_file_is_empty() -> None:
    assert get_session("p", "s") is None
    assert list_cached("p") == {}


def test_roundtrip_and_file_shape() -> None:
    set_session("fixmonc", "auth-research", SessionRef("opencode_session", "ses_1"))
    set_session("fixmonc", "other", SessionRef("context", "ctx_2"))
    set_session("generic", "chat", SessionRef("context", "ctx_3"))

    assert get_session("fixmonc", "auth-research") == SessionRef("opencode_session", "ses_1")
    assert list(list_cached("fixmonc")) == ["auth-research", "other"]
    assert list_cached("generic") == {"chat": SessionRef("context", "ctx_3")}
    assert json.loads(paths.state_file().read_text()) == {
        "sessions": {
            "fixmonc": {
                "auth-research": {"kind": "opencode_session", "id": "ses_1"},
                "other": {"kind": "context", "id": "ctx_2"},
            },
            "generic": {"chat": {"kind": "context", "id": "ctx_3"}},
        }
    }


def test_set_overwrites() -> None:
    set_session("p", "s", SessionRef("context", "a"))
    set_session("p", "s", SessionRef("context", "b"))
    assert get_session("p", "s") == SessionRef("context", "b")


def test_forget_session() -> None:
    set_session("p", "s", SessionRef("context", "a"))
    set_session("p", "t", SessionRef("context", "b"))
    assert forget_session("p", "s") is True
    assert forget_session("p", "s") is False
    assert list_cached("p") == {"t": SessionRef("context", "b")}


def test_forget_peer_only_affects_that_peer() -> None:
    set_session("p", "s", SessionRef("context", "a"))
    set_session("q", "s", SessionRef("context", "b"))
    forget_peer("p")
    assert list_cached("p") == {}
    assert get_session("q", "s") == SessionRef("context", "b")
    forget_peer("unknown")  # no-op, no error


def test_corrupt_file_treated_as_empty_and_warns(capsys: pytest.CaptureFixture[str]) -> None:
    f = paths.state_file()
    f.parent.mkdir(parents=True)
    f.write_text("{garbage")

    assert get_session("p", "s") is None
    out, err = capsys.readouterr()
    assert out == ""
    assert str(f) in err

    # writing recovers the file
    set_session("p", "s", SessionRef("context", "a"))
    assert get_session("p", "s") == SessionRef("context", "a")


def test_invalid_kind_rejected() -> None:
    with pytest.raises(ValueError, match="invalid session kind"):
        SessionRef("bogus", "x")  # type: ignore[arg-type]


def test_entries_with_invalid_kind_on_disk_are_ignored() -> None:
    f = paths.state_file()
    f.parent.mkdir(parents=True)
    f.write_text(
        json.dumps(
            {"sessions": {"p": {"bad": {"kind": "bogus", "id": "x"}, "good": {"kind": "context", "id": "y"}}}}
        )
    )
    assert get_session("p", "bad") is None
    assert list_cached("p") == {"good": SessionRef("context", "y")}


def test_concurrent_writers_do_not_lose_updates() -> None:
    # Two MCP servers / tool calls doing read-modify-write at once must not clobber each other.
    import threading

    names = [f"s{i}" for i in range(40)]
    threads = [threading.Thread(target=set_session, args=("p", n, SessionRef("context", n))) for n in names]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(list_cached("p")) == set(names)
