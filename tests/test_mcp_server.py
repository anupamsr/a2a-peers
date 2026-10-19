from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from a2a_peers import mcp_server, registry, state
from a2a_peers.registry import Peer
from a2a_peers.state import SessionRef


class A2AError(Exception):
    def __init__(self, message: str, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code


@dataclass
class FakeCard:
    name: str = "fake-agent"
    description: str = "a fake"
    opencode: bool = True
    skills: list[dict[str, Any]] = field(default_factory=lambda: [{"id": "code", "name": "Coding"}])

    @property
    def is_opencode(self) -> bool:
        return self.opencode


@dataclass
class FakeAskResult:
    state: str
    text: str = ""
    task_id: str | None = "t1"
    context_id: str | None = None
    interrupt: dict[str, Any] | None = None
    error: str | None = None
    error_type: str | None = None
    session_id: str | None = None


class FakeClient:
    def __init__(
        self,
        card: FakeCard | Exception | None = None,
        sessions: list[dict[str, Any]] | None = None,
        results: list[FakeAskResult] | None = None,
        card_delay: float = 0.0,
    ) -> None:
        self.card = card if card is not None else FakeCard()
        self.sessions = sessions or []
        self.results = list(results or [])
        self.card_delay = card_delay
        self.asks: list[dict[str, Any]] = []
        self.list_calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> FakeClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def aclose(self) -> None:
        return None

    async def get_card(self) -> FakeCard:
        if self.card_delay:
            await asyncio.sleep(self.card_delay)
        if isinstance(self.card, Exception):
            raise self.card
        return self.card

    async def ask(
        self,
        text: str,
        *,
        context_id: str | None = None,
        opencode_session_id: str | None = None,
        model: str | None = None,
        stream: bool = True,
        timeout: float | None = None,
    ) -> FakeAskResult:
        self.asks.append(
            {
                "text": text,
                "context_id": context_id,
                "opencode_session_id": opencode_session_id,
                "model": model,
            }
        )
        return self.results.pop(0)

    async def list_opencode_sessions(
        self, search: str | None = None, limit: int = 20, roots: bool = True
    ) -> list[dict[str, Any]]:
        self.list_calls.append({"search": search, "limit": limit})
        items = self.sessions
        if search is not None:
            items = [s for s in items if search.lower() in (s.get("title") or "").lower()]
        return items[:limit]


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("A2A_PEERS_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.setenv("A2A_PEERS_DATA_DIR", str(tmp_path / "data"))
    registry.upsert_peer(Peer("oc", "http://127.0.0.1:8101", "tok"))
    registry.upsert_peer(Peer("gen", "http://127.0.0.1:8102", None))
    return tmp_path


def use_clients(monkeypatch: pytest.MonkeyPatch, clients: dict[str, FakeClient]) -> None:
    monkeypatch.setattr(mcp_server, "client_factory", lambda peer: clients[peer.name])


async def call(name: str, **args: Any) -> str:
    result = await mcp_server.server.call_tool(name, args)
    assert not result.is_error
    return result.content[0].text


async def test_tools_registered_with_guard_description() -> None:
    tools = await mcp_server.server.list_tools()
    assert {t.name for t in tools} == {"list_peers", "list_sessions", "ask_peer", "forget_session"}
    for t in tools:
        assert t.description.startswith(
            "Only call when the user explicitly asks to contact another agent/session."
        )


async def test_list_peers_reachable_and_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    use_clients(monkeypatch, {"oc": FakeClient(), "gen": FakeClient(card=A2AError("connection refused"))})
    out = await call("list_peers")
    assert "oc: http://127.0.0.1:8101" in out
    assert (
        "reachable: yes" in out and "agent: fake-agent" in out and "opencode: yes" in out and "Coding" in out
    )
    assert "reachable: no (connection refused)" in out


async def test_list_sessions_opencode(monkeypatch: pytest.MonkeyPatch) -> None:
    state.set_session("oc", "cached-one", SessionRef("opencode_session", "ses_c"))
    use_clients(
        monkeypatch,
        {"oc": FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}, {"id": "ses_2", "title": None}])},
    )
    out = await call("list_sessions", peer="oc")
    assert "cached-one -> opencode_session ses_c" in out
    assert "'alpha' id=ses_1" in out and "(untitled) id=ses_2" in out


async def test_list_sessions_uses_cache_title_for_untitled(monkeypatch: pytest.MonkeyPatch) -> None:
    state.set_session("oc", "test", SessionRef("opencode_session", "ses_2"))
    use_clients(
        monkeypatch,
        {"oc": FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}, {"id": "ses_2", "title": None}])},
    )
    out = await call("list_sessions", peer="oc")
    assert "'alpha' id=ses_1" in out and "'test' id=ses_2" in out


async def test_unknown_peer(monkeypatch: pytest.MonkeyPatch) -> None:
    use_clients(monkeypatch, {})
    out = await call("ask_peer", peer="nope", session="s", message="hi")
    assert out.startswith("ERROR:") and "unknown peer 'nope'" in out and "oc" in out and "gen" in out


async def test_ask_opencode_completed_and_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha-x"}, {"id": "ses_2", "title": "alpha"}],
        results=[
            FakeAskResult("TASK_STATE_COMPLETED", "PINEAPPLE"),
            FakeAskResult("TASK_STATE_COMPLETED", ""),
        ],
    )
    use_clients(monkeypatch, {"oc": client})
    assert await call("ask_peer", peer="oc", session="alpha", message="hi") == "PINEAPPLE"
    assert client.asks[0]["opencode_session_id"] == "ses_2"
    assert client.asks[0]["model"] is None
    assert state.get_session("oc", "alpha") == SessionRef("opencode_session", "ses_2")
    n = len(client.list_calls)
    out = await call("ask_peer", peer="oc", session="alpha", message="again")
    assert "empty reply" in out
    assert len(client.list_calls) == n  # cache hit, no listing


async def test_stale_opencode_session_reresolved_once(monkeypatch: pytest.MonkeyPatch) -> None:
    state.set_session("oc", "alpha", SessionRef("opencode_session", "ses_old"))
    client = FakeClient(
        sessions=[{"id": "ses_new", "title": "alpha"}],
        results=[
            FakeAskResult(
                "TASK_STATE_FAILED", error="Session not found", error_type="UPSTREAM_RESOURCE_NOT_FOUND"
            ),
            FakeAskResult("TASK_STATE_COMPLETED", "ok"),
        ],
    )
    use_clients(monkeypatch, {"oc": client})
    assert await call("ask_peer", peer="oc", session="alpha", message="hi") == "ok"
    assert [a["opencode_session_id"] for a in client.asks] == ["ses_old", "ses_new"]
    assert state.get_session("oc", "alpha") == SessionRef("opencode_session", "ses_new")


async def test_fresh_opencode_failure_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}],
        results=[FakeAskResult("TASK_STATE_FAILED", error="gone", error_type="UPSTREAM_RESOURCE_NOT_FOUND")],
    )
    use_clients(monkeypatch, {"oc": client})
    out = await call("ask_peer", peer="oc", session="alpha", message="hi")
    assert out.startswith("ERROR:") and "failed: gone" in out
    assert len(client.asks) == 1


async def test_opencode_session_not_found_lists_titles(monkeypatch: pytest.MonkeyPatch) -> None:
    use_clients(monkeypatch, {"oc": FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}])})
    out = await call("ask_peer", peer="oc", session="gamma", message="hi")
    assert out.startswith("ERROR:") and "'alpha'" in out and "exact title" in out


async def test_input_required_formatting(monkeypatch: pytest.MonkeyPatch) -> None:
    interrupt = {
        "request_id": "per_123",
        "type": "permission",
        "phase": "asked",
        "details": {"permission": "bash", "patterns": ["echo hi"]},
    }
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}],
        results=[FakeAskResult("TASK_STATE_INPUT_REQUIRED", interrupt=interrupt)],
    )
    use_clients(monkeypatch, {"oc": client})
    out = await call("ask_peer", peer="oc", session="alpha", message="run echo hi")
    assert (
        "Peer 'oc' session 'alpha' is waiting for permission approval: bash: echo hi (request id per_123)."
        in out
    )
    assert "Approve or reject it in that session's TUI, then ask again." in out


async def test_timeout_text(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}],
        results=[FakeAskResult("TIMEOUT", error="no reply within 5s")],
    )
    use_clients(monkeypatch, {"oc": client})
    out = await call("ask_peer", peer="oc", session="alpha", message="hi", timeout_seconds=5)
    assert (
        out.startswith("ERROR:")
        and "timed out: no reply within 5s" in out
        and "waiting for an approval" in out
    )


async def test_generic_creates_and_caches_context(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        card=FakeCard(opencode=False),
        results=[
            FakeAskResult("TASK_STATE_COMPLETED", "hello", context_id="ctx_1"),
            FakeAskResult("TASK_STATE_COMPLETED", "again", context_id="ctx_1"),
        ],
    )
    use_clients(monkeypatch, {"gen": client})
    assert await call("ask_peer", peer="gen", session="chat", message="hi") == "hello"
    assert state.get_session("gen", "chat") == SessionRef("context", "ctx_1")
    assert await call("ask_peer", peer="gen", session="chat", message="hi2") == "again"
    assert [a["context_id"] for a in client.asks] == [None, "ctx_1"]


async def test_generic_stale_context_reset(monkeypatch: pytest.MonkeyPatch) -> None:
    state.set_session("gen", "chat", SessionRef("context", "ctx_old"))
    client = FakeClient(
        card=FakeCard(opencode=False),
        results=[
            FakeAskResult("TASK_STATE_FAILED", error="unknown context"),
            FakeAskResult("TASK_STATE_COMPLETED", "fresh", context_id="ctx_new"),
        ],
    )
    use_clients(monkeypatch, {"gen": client})
    out = await call("ask_peer", peer="gen", session="chat", message="hi")
    assert out.startswith("Note:") and out.endswith("fresh")
    assert [a["context_id"] for a in client.asks] == ["ctx_old", None]
    assert state.get_session("gen", "chat") == SessionRef("context", "ctx_new")


async def test_a2a_error_is_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    use_clients(monkeypatch, {"oc": FakeClient(card=A2AError("HTTP 401: unauthorized", status=401))})
    out = await call("ask_peer", peer="oc", session="alpha", message="hi")
    assert out == "ERROR: peer 'oc': HTTP 401: unauthorized"


async def test_forget_session() -> None:
    state.set_session("oc", "alpha", SessionRef("opencode_session", "ses_1"))
    assert "Forgot" in await call("forget_session", peer="oc", session="alpha")
    assert state.get_session("oc", "alpha") is None
    assert "nothing to forget" in await call("forget_session", peer="oc", session="alpha")


async def test_list_sessions_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(peer: str) -> dict:
        raise PermissionError("state.json: permission denied")

    monkeypatch.setattr(state, "list_cached", broken)
    use_clients(monkeypatch, {"oc": FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}])})
    out = await call("list_sessions", peer="oc")
    assert "cannot read session cache" in out and "'alpha' id=ses_1" in out


async def test_list_sessions_bad_registry_is_error_text(isolated_dirs: Path) -> None:
    registry.registry_file().write_text('{"peers": {"oc": {"url": "http://x", "opencode_port": "abc"}}}')
    out = await call("list_sessions", peer="oc")
    assert out.startswith("ERROR:") and "opencode_port" in out


async def test_failed_message_not_doubled(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}],
        results=[FakeAskResult("TASK_STATE_FAILED", error="boom")],
    )
    use_clients(monkeypatch, {"oc": client})
    out = await call("ask_peer", peer="oc", session="alpha", message="hi")
    assert out == "ERROR: peer 'oc' session 'alpha' failed: boom"


async def test_ask_peer_forwards_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(
        sessions=[{"id": "ses_1", "title": "alpha"}],
        results=[FakeAskResult("TASK_STATE_COMPLETED", "ok")],
    )
    use_clients(monkeypatch, {"oc": client})
    assert await call("ask_peer", peer="oc", session="alpha", message="hi", model="llmgw/grok-4.6") == "ok"
    assert client.asks[0]["model"] == "llmgw/grok-4.6"


async def test_ask_peer_rejects_bad_model(monkeypatch: pytest.MonkeyPatch) -> None:
    client = FakeClient(sessions=[{"id": "ses_1", "title": "alpha"}])
    use_clients(monkeypatch, {"oc": client})
    out = await call("ask_peer", peer="oc", session="alpha", message="hi", model="grok-4.6")
    assert out.startswith("ERROR:") and "provider/modelID" in out
    assert client.asks == []
