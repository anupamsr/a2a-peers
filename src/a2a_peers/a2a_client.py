"""A2A 1.0 HTTP+JSON protocol client (async, httpx), tuned for opencode-a2a peers.

See SPIKE.md ("Implications for a2a_client.py") for the empirical protocol rules implemented here.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Self

import httpx

log = logging.getLogger(__name__)

OPENCODE_SESSION_EXT = "urn:opencode-a2a:extension:session-binding:v1"
OPENCODE_EXTENSIONS: list[str] = [
    OPENCODE_SESSION_EXT,
    "urn:opencode-a2a:extension:session-management:v1",
    "urn:opencode-a2a:extension:interactive-interrupt:v1",
    "urn:opencode-a2a:extension:interrupt-recovery:v1",
    "urn:opencode-a2a:extension:stream-hints:v1",
]

A2A_VERSION = "1.0"
CARD_PATH = "/.well-known/agent-card.json"

TERMINAL_STATES = frozenset(
    {"TASK_STATE_COMPLETED", "TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"}
)
INTERRUPT_STATES = frozenset({"TASK_STATE_INPUT_REQUIRED", "TASK_STATE_AUTH_REQUIRED"})
FAILURE_STATES = frozenset({"TASK_STATE_FAILED", "TASK_STATE_CANCELED", "TASK_STATE_REJECTED"})

# JSON-RPC code opencode-a2a returns for upstream opencode errors (e.g. the old `roots` bug).
_UPSTREAM_ERROR_CODE = -32003


def is_loopback_url(url: str) -> bool:
    """True if ``url`` points at this machine (localhost / 127.0.0.0/8 / ::1)."""
    host = httpx.URL(url).host
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class A2AError(Exception):
    """Transport / HTTP / JSON-RPC error. ``str(err)`` is human readable."""

    def __init__(self, message: str, *, status: int | None = None, code: int | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.data: Any = None


@dataclass
class AgentCard:
    raw: dict
    name: str
    description: str
    streaming: bool
    extension_uris: list[str]
    skills: list[dict]  # [{id, name, description}]

    @property
    def is_opencode(self) -> bool:
        return OPENCODE_SESSION_EXT in self.extension_uris

    @classmethod
    def from_json(cls, raw: Any) -> AgentCard:
        if not isinstance(raw, dict):
            raise A2AError("agent card is not a JSON object")
        caps = _as_dict(raw.get("capabilities"))
        uris = [
            ext["uri"]
            for ext in _as_list(caps.get("extensions"))
            if isinstance(ext, dict) and isinstance(ext.get("uri"), str)
        ]
        skills = [
            {
                "id": _as_str(s.get("id")),
                "name": _as_str(s.get("name")),
                "description": _as_str(s.get("description")),
            }
            for s in _as_list(raw.get("skills"))
            if isinstance(s, dict)
        ]
        return cls(
            raw=raw,
            name=_as_str(raw.get("name")),
            description=_as_str(raw.get("description")),
            streaming=caps.get("streaming") is True,
            extension_uris=uris,
            skills=skills,
        )


@dataclass
class AskResult:
    state: str  # final TASK_STATE_*, or "TIMEOUT" / "UNKNOWN"
    text: str
    task_id: str | None
    context_id: str | None
    interrupt: dict | None = None  # set for INPUT_REQUIRED / AUTH_REQUIRED
    error: str | None = None  # failure detail for FAILED/REJECTED/CANCELED/TIMEOUT/UNKNOWN
    error_type: str | None = None  # metadata.opencode.error.type, e.g. UPSTREAM_RESOURCE_NOT_FOUND
    session_id: str | None = None  # metadata.shared.session.id (opencode)


# --------------------------------------------------------------------------- helpers


def _as_dict(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _as_list(v: Any) -> list:
    return v if isinstance(v, list) else []


def _as_str(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _dig(obj: Any, *keys: str) -> Any:
    for k in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(k)
    return obj


def _opt_str(v: Any) -> str | None:
    return v if isinstance(v, str) and v else None


def _parts_text(parts: Any) -> str:
    return "".join(
        p["text"] for p in _as_list(parts) if isinstance(p, dict) and isinstance(p.get("text"), str)
    )


def _normalize_state(state: Any) -> str:
    """Accept both A2A 1.0 (TASK_STATE_COMPLETED) and 0.3 ("completed", "input-required") spellings."""
    if not isinstance(state, str) or not state:
        return "TASK_STATE_UNSPECIFIED"
    s = state.upper().replace("-", "_")
    return s if s.startswith("TASK_STATE_") else f"TASK_STATE_{s}"


def _interrupt_from(metadata: Any, status: Any) -> dict | None:
    raw = _dig(metadata, "shared", "interrupt")
    if not isinstance(raw, dict):
        raw = _dig(status, "message", "metadata", "shared", "interrupt")
    if not isinstance(raw, dict):
        return None
    return {
        "request_id": raw.get("request_id"),
        "type": raw.get("type"),
        "phase": raw.get("phase"),
        "details": raw.get("details"),
    }


def _empty_interrupt() -> dict:
    return {"request_id": None, "type": None, "phase": None, "details": None}


def _artifact_kind(artifact: dict) -> str:
    """Classify an artifact: 'text', 'error', 'ignore' (reasoning/tool) or 'other' (generic peer)."""
    aid = _as_str(artifact.get("artifactId"))
    block_type = _dig(artifact, "metadata", "shared", "stream", "block_type")
    if aid.endswith(":error"):
        return "error"
    if aid.endswith(":stream:text") or block_type == "text":
        return "text"
    if ":stream:reasoning" in aid or ":stream:tool:" in aid or block_type in ("reasoning", "tool_call"):
        return "ignore"
    return "other"


def _http_error_message(resp: httpx.Response, body: bytes) -> str:
    detail = ""
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = None
    err = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(err, dict):
        detail = _as_str(err.get("message"))
        types = [
            t for d in _as_list(err.get("details")) if isinstance(t := _dig(d, "metadata", "type"), str) and t
        ]
        if types:
            detail += f" [{', '.join(types)}]"
    elif isinstance(err, str):
        detail = err
    elif body:
        detail = body[:300].decode("utf-8", "replace").strip()
    msg = f"HTTP {resp.status_code} from {resp.request.url}"
    return f"{msg}: {detail}" if detail else msg


class _StreamState:
    """Accumulates SSE events into an AskResult (SPIKE.md rules)."""

    def __init__(self) -> None:
        self.state: str | None = None
        self.task_id: str | None = None
        self.context_id: str | None = None
        self.session_id: str | None = None
        self.error_type: str | None = None
        self.interrupt: dict | None = None
        self.status_text: str = ""
        self.texts: dict[str, str] = {}  # artifactId -> buffer (insertion ordered)
        self.errors: dict[str, str] = {}
        self.direct_message: dict | None = None

    def _ids(self, obj: dict, task_key: str) -> None:
        self.task_id = _opt_str(obj.get(task_key)) or self.task_id
        self.context_id = _opt_str(obj.get("contextId")) or self.context_id

    def _metadata(self, metadata: Any) -> None:
        self.session_id = _opt_str(_dig(metadata, "shared", "session", "id")) or self.session_id
        self.error_type = _opt_str(_dig(metadata, "opencode", "error", "type")) or self.error_type

    def _status(self, status: Any, metadata: Any) -> None:
        status = _as_dict(status)
        self.state = _normalize_state(status.get("state"))
        msg_text = _parts_text(_dig(status, "message", "parts"))
        if msg_text:
            self.status_text = msg_text
        if self.state in INTERRUPT_STATES:
            self.interrupt = _interrupt_from(metadata, status) or _empty_interrupt()

    def _artifact(self, artifact: Any, append: bool) -> None:
        artifact = _as_dict(artifact)
        kind = _artifact_kind(artifact)
        if kind == "ignore":
            return
        aid = _as_str(artifact.get("artifactId")) or f"_anon{len(self.texts)}"
        chunk = _parts_text(artifact.get("parts"))
        buf = self.errors if kind == "error" else self.texts
        buf[aid] = buf.get(aid, "") + chunk if append else chunk

    def feed(self, event: dict) -> bool:
        """Process one StreamResponse; return True when the ask is finished."""
        if "task" in event:
            task = _as_dict(event["task"])
            self._ids(task, "id")
            self._metadata(task.get("metadata"))
            for art in _as_list(task.get("artifacts")):
                self._artifact(art, append=False)
            self._status(task.get("status"), task.get("metadata"))
        elif "statusUpdate" in event:
            upd = _as_dict(event["statusUpdate"])
            self._ids(upd, "taskId")
            self._metadata(upd.get("metadata"))
            self._status(upd.get("status"), upd.get("metadata"))
        elif "artifactUpdate" in event:
            upd = _as_dict(event["artifactUpdate"])
            self._ids(upd, "taskId")
            self._artifact(upd.get("artifact"), append=upd.get("append") is True)
            return False
        elif "message" in event:
            msg = _as_dict(event["message"])
            self.direct_message = msg
            self.context_id = _opt_str(msg.get("contextId")) or self.context_id
            self.task_id = _opt_str(msg.get("taskId")) or self.task_id
            return True
        else:
            log.debug("ignoring unknown SSE event keys: %s", list(event))
            return False
        return self.state in TERMINAL_STATES or self.state in INTERRUPT_STATES

    def result(self) -> AskResult:
        if self.direct_message is not None:
            return AskResult(
                state="TASK_STATE_COMPLETED",
                text=_parts_text(self.direct_message.get("parts")),
                task_id=self.task_id,
                context_id=self.context_id,
                session_id=_opt_str(_dig(self.direct_message, "metadata", "shared", "session", "id"))
                or self.session_id,
            )
        state = self.state or "UNKNOWN"
        text = "\n".join(t for t in self.texts.values() if t)
        error_text = "\n".join(t for t in self.errors.values() if t)
        error: str | None = None
        if state in FAILURE_STATES:
            error = error_text or self.status_text or f"task ended in {state}"
        elif not text and not (state == "TASK_STATE_COMPLETED" and self.status_text == "Completed."):
            text = self.status_text
        return AskResult(
            state=state,
            text=text,
            task_id=self.task_id,
            context_id=self.context_id,
            interrupt=self.interrupt if state in INTERRUPT_STATES else None,
            error=error,
            error_type=self.error_type,
            session_id=self.session_id,
        )


def _result_from_task(task: dict) -> AskResult:
    status = _as_dict(task.get("status"))
    metadata = task.get("metadata")
    state = _normalize_state(status.get("state"))
    texts: list[str] = []
    errors: list[str] = []
    for art in _as_list(task.get("artifacts")):
        if not isinstance(art, dict):
            continue
        kind = _artifact_kind(art)
        if kind == "ignore":
            continue
        t = _parts_text(art.get("parts"))
        if t:
            (errors if kind == "error" else texts).append(t)
    text = "\n".join(texts)
    status_text = _parts_text(_dig(status, "message", "parts"))
    error: str | None = None
    if state in FAILURE_STATES:
        error = status_text or "\n".join(errors) or f"task ended in {state}"
    elif not text and not (state == "TASK_STATE_COMPLETED" and status_text == "Completed."):
        # On COMPLETED opencode's status.message is a "Completed." placeholder; otherwise use it.
        text = status_text
    interrupt = None
    if state in INTERRUPT_STATES:
        interrupt = _interrupt_from(metadata, status) or _empty_interrupt()
    return AskResult(
        state=state,
        text=text,
        task_id=_opt_str(task.get("id")),
        context_id=_opt_str(task.get("contextId")),
        interrupt=interrupt,
        error=error,
        error_type=_opt_str(_dig(metadata, "opencode", "error", "type")),
        session_id=_opt_str(_dig(metadata, "shared", "session", "id")),
    )


def _result_from_message(msg: dict) -> AskResult:
    return AskResult(
        state="TASK_STATE_COMPLETED",
        text=_parts_text(msg.get("parts")),
        task_id=_opt_str(msg.get("taskId")),
        context_id=_opt_str(msg.get("contextId")),
        session_id=_opt_str(_dig(msg, "metadata", "shared", "session", "id")),
    )


def _result_from_send_response(payload: Any) -> AskResult:
    if isinstance(payload, dict):
        if isinstance(payload.get("task"), dict):
            return _result_from_task(payload["task"])
        if isinstance(payload.get("message"), dict):
            return _result_from_message(payload["message"])
    raise A2AError("unexpected message:send response (neither 'task' nor 'message')")


# --------------------------------------------------------------------------- client


class A2AClient:
    def __init__(
        self,
        base_url: str,
        token: str | None,
        *,
        timeout: float = 1200.0,
        transport: httpx.AsyncBaseTransport | None = None,
        opencode: bool | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._token = token
        self.timeout = timeout
        self._opencode = opencode
        self._card: AgentCard | None = None
        # Don't route loopback peers through HTTP(S)_PROXY: it breaks local peers and
        # would hand the bearer token to the proxy over plain http.
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=httpx.Timeout(timeout),
            trust_env=not is_loopback_url(self.base_url),
        )

    def __repr__(self) -> str:  # never expose the token
        return f"A2AClient({self.base_url!r})"

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    # ---------------------------------------------------------------- plumbing

    def _base_headers(self) -> dict[str, str]:
        headers = {"A2A-Version": A2A_VERSION}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def _is_opencode(self) -> bool:
        if self._opencode is None:
            try:
                await self.get_card()
            except A2AError as e:
                log.debug("card fetch failed, assuming generic peer: %s", e)
                return False
        return bool(self._opencode)

    async def _headers(self) -> dict[str, str]:
        headers = self._base_headers()
        if await self._is_opencode():
            headers["A2A-Extensions"] = ", ".join(OPENCODE_EXTENSIONS)
        return headers

    def _transport_error(self, e: httpx.HTTPError) -> A2AError:
        if isinstance(e, httpx.TimeoutException):
            return A2AError(f"request to peer at {self.base_url} timed out ({type(e).__name__})")
        return A2AError(f"peer unreachable at {self.base_url}: {type(e).__name__}: {e}")

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: Any = None,
        timeout: float | None = None,
    ) -> Any:
        url = self.base_url + path
        kwargs: dict[str, Any] = {"headers": headers}
        if body is not None:
            kwargs["json"] = body
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        try:
            resp = await self._http.request(method, url, **kwargs)
        except httpx.HTTPError as e:
            raise self._transport_error(e) from e
        if not resp.is_success:
            raise A2AError(_http_error_message(resp, resp.content), status=resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise A2AError(f"invalid JSON from {url}", status=resp.status_code) from e

    # ---------------------------------------------------------------- public API

    async def get_card(self) -> AgentCard:
        raw = await self._request_json("GET", CARD_PATH, headers=self._base_headers())
        card = AgentCard.from_json(raw)
        self._card = card
        if self._opencode is None:
            self._opencode = card.is_opencode
        return card

    async def jsonrpc(self, method: str, params: dict) -> Any:
        body = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        payload = await self._request_json("POST", "/", headers=await self._headers(), body=body)
        if not isinstance(payload, dict):
            raise A2AError(f"invalid JSON-RPC response for {method}")
        err = payload.get("error")
        if err is not None:
            err = _as_dict(err)
            code = err.get("code") if isinstance(err.get("code"), int) else None
            msg = _as_str(err.get("message")) or "unknown error"
            exc = A2AError(f"JSON-RPC error {code} from {method}: {msg}", code=code)
            exc.data = err.get("data")
            raise exc
        return payload.get("result")

    async def list_opencode_sessions(
        self, *, search: str | None = None, limit: int = 20, roots: bool = True
    ) -> list[dict]:
        params: dict[str, Any] = {"limit": limit}
        if search:
            params["search"] = search
        if roots:
            params["roots"] = True
        try:
            result = await self.jsonrpc("opencode.sessions.list", params)
        except A2AError as e:
            if not roots or e.code != _UPSTREAM_ERROR_CODE:
                raise
            # Older opencode-a2a sends roots=True verbatim -> opencode 400. Retry without it.
            log.debug("sessions.list with roots failed (%s); retrying without roots", e)
            params.pop("roots")
            result = await self.jsonrpc("opencode.sessions.list", params)
        out: list[dict] = []
        for item in _as_list(_dig(result, "items")):
            entry = self._session_entry(item)
            if entry is not None:
                out.append(entry)
        return out

    async def get_opencode_session(self, session_id: str) -> dict:
        result = await self.jsonrpc("opencode.sessions.get", {"session_id": session_id})
        entry = self._session_entry(_dig(result, "item"))
        if entry is None:
            raise A2AError(f"session {session_id} not found", code=-32001)
        return entry

    @staticmethod
    def _session_entry(item: Any) -> dict | None:
        if not isinstance(item, dict):
            return None
        sid = _opt_str(item.get("id")) or _opt_str(_dig(item, "metadata", "shared", "session", "id"))
        if sid is None:
            return None
        title = _opt_str(_dig(item, "metadata", "shared", "session", "title")) or _opt_str(item.get("title"))
        return {"id": sid, "title": title}

    async def ask(
        self,
        text: str,
        *,
        context_id: str | None = None,
        opencode_session_id: str | None = None,
        stream: bool = True,
        timeout: float | None = None,
    ) -> AskResult:
        limit = self.timeout if timeout is None else timeout
        message: dict[str, Any] = {
            "messageId": str(uuid.uuid4()),
            "role": "ROLE_USER",
            "parts": [{"text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if opencode_session_id:
            message["metadata"] = {"shared": {"session": {"id": opencode_session_id}}}
        body = {"message": message}
        try:
            async with asyncio.timeout(limit):
                headers = await self._headers()
                # Never downgrade opencode peers to message:send: it blocks forever on permission
                # prompts and leaves an interrupt the plugin can't answer (SPIKE.md Q5).
                if (
                    stream
                    and self._card is not None
                    and not self._card.streaming
                    and not self._card.is_opencode
                ):
                    stream = False
                # httpx gets a slightly longer budget so asyncio.timeout is the one that fires.
                if stream:
                    return await self._ask_stream(body, headers, limit + 5)
                payload = await self._request_json(
                    "POST", "/message:send", headers=headers, body=body, timeout=limit + 5
                )
                return _result_from_send_response(payload)
        except (TimeoutError, httpx.TimeoutException):
            return AskResult(
                state="TIMEOUT",
                text="",
                task_id=None,
                context_id=context_id,
                error=f"no reply within {limit:g}s; the peer session may be busy or waiting for approval",
            )

    async def _ask_stream(self, body: dict, headers: dict[str, str], http_timeout: float) -> AskResult:
        url = self.base_url + "/message:stream"
        st = _StreamState()
        headers = {**headers, "Accept": "text/event-stream"}
        try:
            async with self._http.stream(
                "POST", url, headers=headers, json=body, timeout=httpx.Timeout(http_timeout)
            ) as resp:
                if not resp.is_success:
                    raise A2AError(_http_error_message(resp, await resp.aread()), status=resp.status_code)
                if "text/event-stream" not in resp.headers.get("content-type", "text/event-stream"):
                    # Server answered with plain JSON instead of SSE.
                    try:
                        payload = json.loads(await resp.aread())
                    except ValueError as e:
                        raise A2AError(f"invalid JSON from {url}", status=resp.status_code) from e
                    return _result_from_send_response(payload)
                async for event in _iter_sse(resp):
                    if st.feed(event):
                        return st.result()
        except httpx.TimeoutException:
            raise
        except httpx.HTTPError as e:
            raise self._transport_error(e) from e
        return await self._after_eof(st)

    async def _after_eof(self, st: _StreamState) -> AskResult:
        """Stream ended without a terminal/interrupt status: consult GET /tasks/{id}."""
        partial = st.result()
        if st.task_id is None:
            partial.state = "UNKNOWN"
            partial.error = "stream ended before any task was reported"
            return partial
        try:
            task = await self._request_json("GET", f"/tasks/{st.task_id}", headers=await self._headers())
        except A2AError as e:
            partial.state = "UNKNOWN"
            partial.error = f"stream ended without a final status and task lookup failed: {e}"
            return partial
        task = task.get("task", task) if isinstance(task, dict) else None
        if not isinstance(task, dict):
            partial.state = "UNKNOWN"
            partial.error = "stream ended without a final status and task lookup returned no task"
            return partial
        res = _result_from_task(task)
        if not res.text:
            res.text = partial.text
        res.task_id = res.task_id or st.task_id
        res.context_id = res.context_id or st.context_id
        res.session_id = res.session_id or st.session_id
        res.error_type = res.error_type or st.error_type
        return res


async def _iter_sse(resp: httpx.Response) -> AsyncIterator[dict]:
    """Yield decoded JSON payloads from an SSE body. Ignores comments and non-data fields."""
    data_lines: list[str] = []

    def flush() -> dict | None:
        if not data_lines:
            return None
        raw = "\n".join(data_lines)
        data_lines.clear()
        try:
            obj = json.loads(raw)
        except ValueError:
            log.debug("skipping non-JSON SSE data (%d bytes)", len(raw))
            return None
        return obj if isinstance(obj, dict) else None

    async for line in resp.aiter_lines():
        line = line.rstrip("\r")
        if line == "":
            if (ev := flush()) is not None:
                yield ev
            continue
        if line.startswith(":"):
            continue
        fname, _, value = line.partition(":")
        if fname == "data":
            data_lines.append(value.removeprefix(" "))
    if (ev := flush()) is not None:
        yield ev


__all__ = [
    "OPENCODE_EXTENSIONS",
    "OPENCODE_SESSION_EXT",
    "A2AClient",
    "A2AError",
    "AgentCard",
    "AskResult",
]
