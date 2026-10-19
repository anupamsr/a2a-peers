from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from a2a_peers import paths
from a2a_peers.registry import Peer, RegistryError, get_peer, load_peers, remove_peer, upsert_peer


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("A2A_PEERS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("A2A_PEERS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


def test_missing_file_yields_no_peers() -> None:
    assert load_peers() == {}


def test_roundtrip_preserves_fields_and_order() -> None:
    a = Peer(name="fixmonc", url="http://127.0.0.1:8101/", token="abc", workspace="/w", opencode_port=4101)
    b = Peer(name="other", url="https://example.test/a2a", token=None)
    upsert_peer(a)
    upsert_peer(b)

    peers = load_peers()
    assert list(peers) == ["fixmonc", "other"]
    assert peers["fixmonc"] == Peer("fixmonc", "http://127.0.0.1:8101", "abc", "/w", 4101)
    assert peers["other"] == b

    raw = json.loads(paths.registry_file().read_text())
    assert raw == {
        "peers": {
            "fixmonc": {
                "url": "http://127.0.0.1:8101",
                "token": "abc",
                "workspace": "/w",
                "opencode_port": 4101,
            },
            "other": {"url": "https://example.test/a2a", "token": None},
        }
    }


def test_upsert_replaces_existing_peer() -> None:
    upsert_peer(Peer("p", "http://a", "t1"))
    upsert_peer(Peer("p", "http://b", "t2"))
    assert load_peers() == {"p": Peer("p", "http://b", "t2")}


def test_write_uses_private_permissions() -> None:
    upsert_peer(Peer("p", "http://a", "secret"))
    f = paths.registry_file()
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    # no leftover temp files
    assert not [p.name for p in f.parent.iterdir() if p.name.endswith(".tmp")]


def test_rewrite_fixes_loose_permissions(capsys: pytest.CaptureFixture[str]) -> None:
    upsert_peer(Peer("p", "http://a", "secret"))
    os.chmod(paths.registry_file(), 0o644)
    upsert_peer(Peer("q", "http://b", None))
    assert stat.S_IMODE(paths.registry_file().stat().st_mode) == 0o600


def test_loose_permissions_warn_on_stderr_not_stdout(capsys: pytest.CaptureFixture[str]) -> None:
    upsert_peer(Peer("p", "http://a", "secret"))
    os.chmod(paths.registry_file(), 0o644)
    load_peers()
    out, err = capsys.readouterr()
    assert out == ""
    assert "0600" in err


def test_private_permissions_do_not_warn(capsys: pytest.CaptureFixture[str]) -> None:
    upsert_peer(Peer("p", "http://a", "secret"))
    load_peers()
    assert capsys.readouterr().err == ""


def test_unknown_peer_lists_known_peers() -> None:
    upsert_peer(Peer("a", "http://a", None))
    upsert_peer(Peer("b", "http://b", None))
    with pytest.raises(RegistryError) as exc:
        get_peer("x")
    assert str(exc.value) == "unknown peer 'x'; known: a, b"


def test_get_peer_returns_peer() -> None:
    upsert_peer(Peer("a", "http://a/", "t"))
    assert get_peer("a") == Peer("a", "http://a", "t")


def test_remove_peer() -> None:
    upsert_peer(Peer("a", "http://a", None))
    upsert_peer(Peer("b", "http://b", None))
    assert remove_peer("a") is True
    assert remove_peer("a") is False
    assert list(load_peers()) == ["b"]


def test_remove_peer_on_missing_file_returns_false() -> None:
    assert remove_peer("nope") is False
    assert not paths.registry_file().exists()


def test_invalid_json_raises_with_path() -> None:
    f = paths.registry_file()
    f.parent.mkdir(parents=True)
    f.write_text("{not json")
    os.chmod(f, 0o600)
    with pytest.raises(RegistryError) as exc:
        load_peers()
    assert str(f) in str(exc.value)


def test_non_integer_opencode_port_is_registry_error() -> None:
    upsert_peer(Peer("a", "http://a", None))
    f = paths.registry_file()
    f.write_text(json.dumps({"peers": {"a": {"url": "http://a", "token": None, "opencode_port": "abc"}}}))
    with pytest.raises(RegistryError, match="opencode_port"):
        load_peers()
