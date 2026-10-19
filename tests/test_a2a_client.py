from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest

from a2a_peers.a2a_client import (
    OPENCODE_EXTENSIONS,
    OPENCODE_SESSION_EXT,
    A2AClient,
    A2AError,
    is_loopback_url,
)

BASE = "http://peer.test"
TASK = "379bf522-task"
CTX = "668a76fb-ctx"
SES = "ses_ecffdf456ffe7nJybOJkup472R"

OPENCODE_CARD = {
    "name": "opencode",
    "description": "OpenCode agent",
    "capabilities": {
        "streaming": True,
        "extensions": [{"uri": OPENCODE_SESSION_EXT}, {"uri": "urn:opencode-a2a:extension:stream-hints:v1"}],
    },
    "skills": [{"id": "code", "name": "Coding", "description": "writes code", "tags": ["x"]}],
}
GENERIC_CARD = {
    "name": "generic",
    "description": "plain agent",
    "capabilities": {"streaming": False},
    "skills": [],
}

Handler = Callable[[httpx.Request], Awaitable[httpx.Response] | httpx.Response]


def sse(*events: dict | str) -> bytes:
    """Build an SSE body; str items are emitted verbatim (comments etc.)."""
    out = []
    for ev in events:
        out.append(ev if isinstance(ev, str) else f"data: {json.dumps(ev)}\n\n")
    return "".join(out).encode()


def sse_response(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


class Recorder:
    def __init__(self, routes: dict[tuple[str, str], Handler]) -> None:
        self.routes = routes
        self.requests: list[httpx.Request] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            return httpx.Response(404, json={"error": {"message": "no route"}})
        res = handler(request)
        if asyncio.iscoroutine(res):
            res = await res
        return res  # type: ignore[return-value]

    def last(self, method: str, path: str) -> httpx.Request:
        return [r for r in self.requests if r.method == method and r.url.path == path][-1]

    def bodies(self, method: str, path: str) -> list[Any]:
        return [json.loads(r.content) for r in self.requests if r.method == method and r.url.path == path]


def make_client(
    routes: dict[tuple[str, str], Handler], *, token: str | None = "secret", **kw: Any
) -> tuple[A2AClient, Recorder]:
    rec = Recorder(routes)
    return A2AClient(BASE, token, transport=httpx.MockTransport(rec), **kw), rec


def card_route(card: dict) -> dict[tuple[str, str], Handler]:
    return {("GET", "/.well-known/agent-card.json"): lambda r: httpx.Response(200, json=card)}


def task_event(state: str = "TASK_STATE_WORKING") -> dict:
    return {"task": {"id": TASK, "contextId": CTX, "status": {"state": state}, "history": []}}


def text_chunk(text: str, *, append: bool = False, last: bool = False, hints: bool = True) -> dict:
    art: dict[str, Any] = {"artifactId": f"{TASK}:stream:text", "parts": [{"text": text}]}
    if hints:
        art["metadata"] = {"shared": {"stream": {"block_type": "text", "message_id": "msg_1"}}}
    upd: dict[str, Any] = {"taskId": TASK, "contextId": CTX, "artifact": art}
    if append:
        upd["append"] = True
    if last:
        upd["lastChunk"] = True
    return {"artifactUpdate": upd}


def status_event(state: str, metadata: dict | None = None, message: dict | None = None) -> dict:
    status: dict[str, Any] = {"state": state}
    if message:
        status["message"] = message
    upd: dict[str, Any] = {"taskId": TASK, "contextId": CTX, "status": status}
    if metadata is not None:
        upd["metadata"] = metadata
    return {"statusUpdate": upd}


# --------------------------------------------------------------------------- card / headers


async def test_get_card_parses_opencode_card() -> None:
    client, _ = make_client(card_route(OPENCODE_CARD))
    async with client:
        card = await client.get_card()
    assert card.name == "opencode"
    assert card.streaming is True
    assert card.is_opencode
    assert card.skills == [{"id": "code", "name": "Coding", "description": "writes code"}]


async def test_get_card_is_defensive_about_garbage_fields() -> None:
    client, _ = make_client(card_route({"name": 5, "capabilities": "nope", "skills": [1, {"id": "a"}]}))
    async with client:
        card = await client.get_card()
    assert card.name == ""
    assert card.streaming is False
    assert not card.is_opencode
    assert card.skills == [{"id": "a", "name": "", "description": ""}]


async def test_opencode_peer_gets_extension_and_auth_headers() -> None:
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(
        sse(task_event(), text_chunk("hi", last=True), status_event("TASK_STATE_COMPLETED"))
    )
    client, rec = make_client(routes)
    async with client:
        res = await client.ask("hello", opencode_session_id=SES)
    assert res.state == "TASK_STATE_COMPLETED"
    req = rec.last("POST", "/message:stream")
    assert req.headers["Authorization"] == "Bearer secret"
    assert req.headers["A2A-Version"] == "1.0"
    assert req.headers["A2A-Extensions"] == ", ".join(OPENCODE_EXTENSIONS)
    assert len(OPENCODE_EXTENSIONS) == 5
    msg = json.loads(req.content)["message"]
    assert msg["role"] == "ROLE_USER"
    assert msg["parts"] == [{"text": "hello"}]
    assert msg["metadata"] == {"shared": {"session": {"id": SES}}}
    assert msg["messageId"]
    assert "contextId" not in msg


async def test_generic_peer_no_extensions_no_auth_and_falls_back_to_send() -> None:
    routes = card_route(GENERIC_CARD)
    routes[("POST", "/message:send")] = lambda r: httpx.Response(
        200, json={"message": {"role": "ROLE_AGENT", "contextId": "c-9", "parts": [{"text": "hey"}]}}
    )
    client, rec = make_client(routes, token=None)
    async with client:
        res = await client.ask("hi", context_id="c-9")
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "hey"
    assert res.context_id == "c-9"
    req = rec.last("POST", "/message:send")
    assert "A2A-Extensions" not in req.headers
    assert "Authorization" not in req.headers
    assert req.headers["A2A-Version"] == "1.0"
    assert json.loads(req.content)["message"]["contextId"] == "c-9"
    assert not any(r.url.path == "/message:stream" for r in rec.requests)


async def test_explicit_opencode_flag_skips_card_fetch() -> None:
    routes = {
        ("POST", "/"): lambda r: httpx.Response(
            200, json={"jsonrpc": "2.0", "id": 1, "result": {"items": []}}
        )
    }
    client, rec = make_client(routes, opencode=True)
    async with client:
        assert await client.list_opencode_sessions() == []
    assert [r.url.path for r in rec.requests] == ["/"]
    assert rec.requests[0].headers["A2A-Extensions"] == ", ".join(OPENCODE_EXTENSIONS)


# --------------------------------------------------------------------------- streaming


async def test_stream_completed_with_deltas_reasoning_tool_and_last_chunk_replace() -> None:
    body = sse(
        task_event(),
        ": ping - 2026-10-12 10:00:00\n\n",
        status_event("TASK_STATE_WORKING", {"shared": {"progress": {"type": "step-start"}}}),
        text_chunk("I'll read "),
        text_chunk("b.txt now.", append=True),
        {
            "artifactUpdate": {
                "taskId": TASK,
                "contextId": CTX,
                "artifact": {"artifactId": f"{TASK}:stream:reasoning", "parts": [{"text": "thinking hard"}]},
            }
        },
        {
            "artifactUpdate": {
                "taskId": TASK,
                "contextId": CTX,
                "artifact": {
                    "artifactId": f"{TASK}:stream:tool:prt_1",
                    "parts": [
                        {
                            "data": {
                                "call_id": "c1",
                                "tool": "read",
                                "status": "completed",
                                "output": "beta notes",
                            }
                        }
                    ],
                },
            }
        },
        ": ping - 2026-10-12 10:00:15\n\n",
        text_chunk("1: beta", hints=False),
        text_chunk(" notes", append=True, hints=False),
        text_chunk("1: beta notes", last=True),
        status_event("TASK_STATE_COMPLETED", {"shared": {"session": {"id": SES}, "usage": {"total": 3}}}),
    )
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("read b.txt", opencode_session_id=SES)
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "1: beta notes"
    assert res.task_id == TASK
    assert res.context_id == CTX
    assert res.session_id == SES
    assert res.error is None and res.interrupt is None


async def test_stream_multiline_data_event_is_joined() -> None:
    payload = json.dumps(status_event("TASK_STATE_COMPLETED"), indent=1).splitlines()
    multi = "".join(f"data: {line}\n" for line in payload) + "\n"
    body = sse(task_event(), text_chunk("ok", last=True), multi)
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x")
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "ok"


async def test_stream_input_required_returns_interrupt() -> None:
    interrupt = {
        "request_id": "per_13002edb5001QItu4xQHK8bynF",
        "type": "permission",
        "phase": "asked",
        "details": {"permission": "bash", "patterns": ["echo hi"]},
    }
    body = sse(
        task_event(),
        {
            "artifactUpdate": {
                "taskId": TASK,
                "contextId": CTX,
                "artifact": {
                    "artifactId": f"{TASK}:stream:tool:prt_x",
                    "parts": [
                        {"data": {"tool": "bash", "status": "running", "input": {"command": "echo hi"}}}
                    ],
                },
            }
        },
        status_event(
            "TASK_STATE_INPUT_REQUIRED", {"shared": {"session": {"id": SES}, "interrupt": interrupt}}
        ),
        # anything after the interrupt must not be consumed
        status_event("TASK_STATE_COMPLETED"),
    )
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("Run the bash command: echo hi", opencode_session_id=SES)
    assert res.state == "TASK_STATE_INPUT_REQUIRED"
    assert res.interrupt == interrupt
    assert res.session_id == SES


async def test_stream_interrupt_fallback_to_status_message_metadata() -> None:
    interrupt = {"request_id": "q_1", "type": "question", "phase": "asked", "details": {"q": "?"}}
    body = sse(
        task_event(),
        status_event(
            "TASK_STATE_AUTH_REQUIRED",
            None,
            {"role": "ROLE_AGENT", "parts": [], "metadata": {"shared": {"interrupt": interrupt}}},
        ),
    )
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x")
    assert res.state == "TASK_STATE_AUTH_REQUIRED"
    assert res.interrupt == interrupt


async def test_stream_failed_with_error_artifact_and_error_type() -> None:
    err_text = (
        "OpenCode rejected the request because the target resource was not found "
        "(send_message, status=404, detail=Session not found: ses_doesnotexist)."
    )
    body = sse(
        task_event(),
        {
            "artifactUpdate": {
                "taskId": TASK,
                "contextId": CTX,
                "lastChunk": True,
                "artifact": {"artifactId": f"{TASK}:error", "parts": [{"text": err_text}]},
            }
        },
        status_event(
            "TASK_STATE_FAILED",
            {"opencode": {"error": {"upstream_status": 404.0, "type": "UPSTREAM_RESOURCE_NOT_FOUND"}}},
        ),
    )
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x", opencode_session_id="ses_doesnotexist")
    assert res.state == "TASK_STATE_FAILED"
    assert res.error == err_text
    assert res.error_type == "UPSTREAM_RESOURCE_NOT_FOUND"
    assert res.text == ""


async def test_stream_direct_message_reply_is_completed() -> None:
    body = sse(
        {
            "message": {
                "messageId": "m",
                "role": "ROLE_AGENT",
                "contextId": "c1",
                "parts": [{"text": "hello there"}],
            }
        }
    )
    routes = {("POST", "/message:stream"): lambda r: sse_response(body)}
    client, _ = make_client(routes, opencode=False)
    async with client:
        res = await client.ask("hi")
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "hello there"
    assert res.context_id == "c1"


async def test_stream_eof_without_terminal_falls_back_to_get_task() -> None:
    body = sse(task_event(), text_chunk("partial"))
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    routes[("GET", f"/tasks/{TASK}")] = lambda r: httpx.Response(
        200,
        json={
            "id": TASK,
            "contextId": CTX,
            "status": {"state": "TASK_STATE_COMPLETED"},
            "artifacts": [{"artifactId": "a", "name": "response", "parts": [{"text": "full answer"}]}],
        },
    )
    client, rec = make_client(routes)
    async with client:
        res = await client.ask("x")
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "full answer"
    assert rec.last("GET", f"/tasks/{TASK}").headers["A2A-Version"] == "1.0"


async def test_stream_eof_and_task_lookup_fails_gives_unknown() -> None:
    body = sse(task_event(), text_chunk("partial"))
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: sse_response(body)
    routes[("GET", f"/tasks/{TASK}")] = lambda r: httpx.Response(500, json={"error": {"message": "boom"}})
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x")
    assert res.state == "UNKNOWN"
    assert res.text == "partial"
    assert res.error and "boom" in res.error


# --------------------------------------------------------------------------- message:send


async def test_send_completed_uses_artifacts_not_placeholder() -> None:
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:send")] = lambda r: httpx.Response(
        200,
        json={
            "task": {
                "id": "bdf4",
                "contextId": "52288a13",
                "status": {
                    "state": "TASK_STATE_COMPLETED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": "Completed."}]},
                },
                "artifacts": [{"artifactId": "7c62", "name": "response", "parts": [{"text": "PINEAPPLE"}]}],
                "metadata": {"shared": {"session": {"id": "ses_ed00"}}},
            }
        },
    )
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("Reply with PINEAPPLE", opencode_session_id="ses_ed00", stream=False)
    assert res.state == "TASK_STATE_COMPLETED"
    assert res.text == "PINEAPPLE"
    assert res.task_id == "bdf4"
    assert res.session_id == "ses_ed00"


async def test_send_failed_unknown_session() -> None:
    msg = "OpenCode rejected the request because the target resource was not found (Session not found)."
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:send")] = lambda r: httpx.Response(
        200,
        json={
            "task": {
                "id": "t",
                "contextId": "c",
                "status": {
                    "state": "TASK_STATE_FAILED",
                    "message": {"role": "ROLE_AGENT", "parts": [{"text": msg}]},
                },
                "metadata": {
                    "opencode": {"error": {"upstream_status": 404.0, "type": "UPSTREAM_RESOURCE_NOT_FOUND"}}
                },
            }
        },
    )
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x", opencode_session_id="ses_doesnotexist", stream=False)
    assert res.state == "TASK_STATE_FAILED"
    assert res.error == msg
    assert res.error_type == "UPSTREAM_RESOURCE_NOT_FOUND"
    assert res.text == ""


# --------------------------------------------------------------------------- errors / timeout


async def test_http_401_raises_with_status() -> None:
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = lambda r: httpx.Response(
        401, json={"error": {"message": "Unauthorized"}}
    )
    client, _ = make_client(routes)
    async with client:
        with pytest.raises(A2AError) as ei:
            await client.ask("x")
    assert ei.value.status == 401
    assert "Unauthorized" in str(ei.value)
    assert "secret" not in str(ei.value)


async def test_http_400_includes_details_type() -> None:
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:send")] = lambda r: httpx.Response(
        400,
        json={
            "error": {
                "code": 400,
                "status": "FAILED_PRECONDITION",
                "message": "Request requires explicit A2A extension negotiation via A2A-Extensions.",
                "details": [
                    {
                        "reason": "UNSUPPORTED_OPERATION",
                        "metadata": {"type": "EXTENSION_NEGOTIATION_REQUIRED"},
                    }
                ],
            }
        },
    )
    client, _ = make_client(routes)
    async with client:
        with pytest.raises(A2AError) as ei:
            await client.ask("x", stream=False)
    assert ei.value.status == 400
    assert "EXTENSION_NEGOTIATION_REQUIRED" in str(ei.value)
    assert "explicit A2A extension negotiation" in str(ei.value)


async def test_connection_error_says_unreachable() -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    client = A2AClient(BASE, "t", transport=httpx.MockTransport(boom), opencode=True)
    async with client:
        with pytest.raises(A2AError) as ei:
            await client.ask("x")
    assert "peer unreachable at http://peer.test" in str(ei.value)


async def test_timeout_returns_timeout_result() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={})

    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/message:stream")] = slow
    client, _ = make_client(routes)
    async with client:
        res = await client.ask("x", timeout=0.2)
    assert res.state == "TIMEOUT"
    assert res.error and "0.2s" in res.error and "waiting for approval" in res.error


# --------------------------------------------------------------------------- JSON-RPC / sessions


def rpc_ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "result": result})


def rpc_err(code: int, message: str, data: Any = None) -> httpx.Response:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return httpx.Response(200, json={"jsonrpc": "2.0", "id": 1, "error": err})


async def test_jsonrpc_error_raises_with_code() -> None:
    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/")] = lambda r: rpc_err(-32004, "Method x requires explicit A2A extension negotiation")
    client, rec = make_client(routes)
    async with client:
        with pytest.raises(A2AError) as ei:
            await client.jsonrpc("opencode.permissions.list", {})
    assert ei.value.code == -32004
    assert "extension negotiation" in str(ei.value)
    body = rec.bodies("POST", "/")[0]
    assert body["jsonrpc"] == "2.0" and body["method"] == "opencode.permissions.list"


async def test_list_sessions_extracts_titles() -> None:
    items = [
        {
            "id": "ses_a",
            "contextId": "ctx:opencode-session:ses_a",
            "status": {"state": "TASK_STATE_COMPLETED"},
            "metadata": {"shared": {"session": {"id": "ses_a", "title": "spike-alpha"}}},
        },
        {"id": "ses_b", "metadata": {"shared": {"session": {"id": "ses_b"}}}},
    ]
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        if body["method"] == "opencode.sessions.get":
            return rpc_ok(
                {"item": {"id": "ses_b", "metadata": {"shared": {"session": {"id": "ses_b", "title": "beta"}}}}}
            )
        return rpc_ok({"items": items})

    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/")] = handler
    client, rec = make_client(routes)
    async with client:
        out = await client.list_opencode_sessions(search="spike", limit=5)
    assert out == [{"id": "ses_a", "title": "spike-alpha"}, {"id": "ses_b", "title": "beta"}]
    bodies = rec.bodies("POST", "/")
    assert bodies[0]["method"] == "opencode.sessions.list"
    assert bodies[0]["params"] == {"limit": 5, "search": "spike", "roots": True}
    assert bodies[1]["method"] == "opencode.sessions.get"
    assert bodies[1]["params"] == {"session_id": "ses_b"}


async def test_list_sessions_retries_without_roots_on_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = json.loads(request.content)["params"]
        if "roots" in params:
            return rpc_err(-32003, "Upstream OpenCode error", [{"upstreamStatus": "400"}])
        return rpc_ok({"items": [{"id": "ses_a"}]})

    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/")] = handler
    client, rec = make_client(routes)
    async with client:
        out = await client.list_opencode_sessions()
    assert out == [{"id": "ses_a", "title": None}]
    listed = [b for b in rec.bodies("POST", "/") if b["method"] == "opencode.sessions.list"]
    assert [b["params"] for b in listed] == [{"limit": 20, "roots": True}, {"limit": 20}]


async def test_get_session_and_not_found() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        sid = json.loads(request.content)["params"]["session_id"]
        if sid == "ses_a":
            return rpc_ok(
                {"item": {"id": "ses_a", "metadata": {"shared": {"session": {"id": "ses_a", "title": "T"}}}}}
            )
        return rpc_err(-32001, "Session not found")

    routes = card_route(OPENCODE_CARD)
    routes[("POST", "/")] = handler
    client, _ = make_client(routes)
    async with client:
        assert await client.get_opencode_session("ses_a") == {"id": "ses_a", "title": "T"}
        with pytest.raises(A2AError) as ei:
            await client.get_opencode_session("ses_nope")
    assert ei.value.code == -32001


async def test_opencode_peer_streams_even_if_card_says_no_streaming() -> None:
    card = {**OPENCODE_CARD, "capabilities": {**OPENCODE_CARD["capabilities"], "streaming": False}}
    routes = card_route(card)
    routes[("POST", "/message:stream")] = lambda r: sse_response(
        sse(task_event(), text_chunk("hi", last=True), status_event("TASK_STATE_COMPLETED"))
    )
    client, rec = make_client(routes)
    async with client:
        res = await client.ask("x", opencode_session_id=SES)
    assert res.text == "hi"
    assert not any(r.url.path == "/message:send" for r in rec.requests)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://127.0.0.1:8101", True),
        ("http://127.5.0.1", True),
        ("http://localhost:9000", True),
        ("http://[::1]:9000", True),
        ("https://agents.example.com", False),
        ("http://10.0.0.5:8000", False),
    ],
)
def test_is_loopback_url(url: str, expected: bool) -> None:
    assert is_loopback_url(url) is expected
