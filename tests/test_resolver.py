from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from a2a_peers import resolver, state
from a2a_peers.resolver import AmbiguousSession, SessionNotFound
from a2a_peers.state import SessionRef


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("A2A_PEERS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("A2A_PEERS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@dataclass
class FakeCard:
    opencode: bool = True

    @property
    def is_opencode(self) -> bool:
        return self.opencode


@dataclass
class FakeClient:
    sessions: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def list_opencode_sessions(
        self, search: str | None = None, limit: int = 20, roots: bool = True
    ) -> list[dict[str, Any]]:
        self.calls.append({"search": search, "limit": limit})
        items = self.sessions
        if search is not None:
            items = [s for s in items if search.lower() in (s.get("title") or s.get("_hidden", "")).lower()]
        return [{"id": s["id"], "title": s.get("title")} for s in items][:limit]


async def test_cache_hit_skips_listing() -> None:
    state.set_session("p", "alpha", SessionRef("opencode_session", "ses_cached"))
    client = FakeClient(sessions=[{"id": "ses_other", "title": "alpha"}])
    ref = await resolver.resolve(client, FakeCard(), "p", "alpha", create=False)
    assert ref == SessionRef("opencode_session", "ses_cached")
    assert client.calls == []


async def test_exact_title_among_substring_hits_is_cached() -> None:
    client = FakeClient(
        sessions=[
            {"id": "ses_1", "title": "alpha-2"},
            {"id": "ses_2", "title": "alpha"},
            {"id": "ses_3", "title": "Alpha"},
        ]
    )
    ref = await resolver.resolve(client, FakeCard(), "p", "alpha", create=False)
    assert ref == SessionRef("opencode_session", "ses_2")
    assert client.calls[0] == {"search": "alpha", "limit": 50}
    assert state.get_session("p", "alpha") == ref


async def test_duplicate_exact_titles_are_ambiguous() -> None:
    client = FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}, {"id": "ses_2", "title": "alpha"}])
    with pytest.raises(AmbiguousSession) as exc:
        await resolver.resolve(client, FakeCard(), "p", "alpha", create=False)
    assert "ses_1" in str(exc.value) and "ses_2" in str(exc.value)
    assert state.get_session("p", "alpha") is None


async def test_untitled_single_hit_matches_multiple_ambiguous() -> None:
    client = FakeClient(sessions=[{"id": "ses_1", "_hidden": "spike-alpha"}])
    ref = await resolver.resolve(client, FakeCard(), "p", "alpha", create=False)
    assert ref == SessionRef("opencode_session", "ses_1")

    client = FakeClient(sessions=[{"id": "ses_1", "_hidden": "alpha"}, {"id": "ses_2", "_hidden": "alpha-2"}])
    with pytest.raises(AmbiguousSession, match="ses_1, ses_2"):
        await resolver.resolve(client, FakeCard(), "p2", "alpha", create=False)


async def test_not_found_lists_titles() -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}, {"id": "ses_2"}, {"id": "ses_3", "title": "beta"}]
    )
    with pytest.raises(SessionNotFound) as exc:
        await resolver.resolve(client, FakeCard(), "p", "gamma", create=False)
    msg = str(exc.value)
    assert "'alpha'" in msg and "'beta'" in msg
    assert "exact title in the peer's TUI" in msg
    assert client.calls[-1] == {"search": None, "limit": 20}


async def test_generic_peer_create_and_not_found() -> None:
    client = FakeClient()
    assert await resolver.resolve(client, FakeCard(opencode=False), "g", "chat", create=True) is None
    state.set_session("g", "known", SessionRef("context", "ctx_1"))
    with pytest.raises(SessionNotFound, match="'known'"):
        await resolver.resolve(client, FakeCard(opencode=False), "g", "chat", create=False)
    assert client.calls == []


async def test_invalidate() -> None:
    state.set_session("p", "alpha", SessionRef("opencode_session", "ses_1"))
    assert resolver.invalidate("p", "alpha") is True
    assert state.get_session("p", "alpha") is None
    assert resolver.invalidate("p", "alpha") is False
