# Internal module contracts (for parallel implementation)

Python >= 3.11, `from __future__ import annotations`, type hints everywhere, stdlib + `httpx` + `mcp` (v2.3, `from mcp.server.mcpserver import MCPServer`) only.
Use `a2a_peers.paths` for every filesystem location (tests override via `A2A_PEERS_CONFIG_DIR` / `A2A_PEERS_DATA_DIR`).
JSON files: write atomically (tmp file + `os.replace`), `indent=2`, keep insertion order.

## registry.py  — peers.json (mode 0600)

```json
{"peers": {"fixmonc": {"url": "http://127.0.0.1:8101", "token": "abc...", "workspace": "/abs/path", "opencode_port": 4101}}}
```
`workspace` and `opencode_port` are optional (set only for peers created by the launcher).

```python
@dataclass(frozen=True)
class Peer:
    name: str
    url: str            # A2A base URL, no trailing slash
    token: str | None   # bearer token; None = no auth
    workspace: str | None = None
    opencode_port: int | None = None

class RegistryError(Exception): ...

def load_peers() -> dict[str, Peer]            # missing file -> {}; warns (stderr) if mode is not 0600
def get_peer(name: str) -> Peer                # raises RegistryError("unknown peer 'x'; known: a, b")
def upsert_peer(peer: Peer) -> None            # creates dirs, writes with mode 0600
def remove_peer(name: str) -> bool
```

## state.py — state.json (session-name cache)

```json
{"sessions": {"fixmonc": {"auth-research": {"kind": "opencode_session", "id": "ses_..."}}}}
```
`kind` is `"opencode_session"` (id = opencode session id, sent as metadata.shared.session.id)
or `"context"` (id = A2A contextId, sent as message.contextId).

```python
@dataclass(frozen=True)
class SessionRef:
    kind: Literal["opencode_session", "context"]
    id: str

def get_session(peer: str, name: str) -> SessionRef | None
def set_session(peer: str, name: str, ref: SessionRef) -> None
def forget_session(peer: str, name: str) -> bool
def list_cached(peer: str) -> dict[str, SessionRef]
def forget_peer(peer: str) -> None
```

## IMPORTANT: corrections from SPIKE.md (authoritative where it differs from below)
- Every request: `Authorization: Bearer <t>`, `A2A-Version: 1.0`; for opencode peers also
  `A2A-Extensions: urn:opencode-a2a:extension:session-binding:v1, urn:opencode-a2a:extension:model-selection:v1, urn:opencode-a2a:extension:session-management:v1, urn:opencode-a2a:extension:interactive-interrupt:v1, urn:opencode-a2a:extension:interrupt-recovery:v1, urn:opencode-a2a:extension:stream-hints:v1`.
- We patched opencode-a2a (local editable install): `roots` now works, and `opencode.sessions.list` / `.get`
  items carry the title at `item.metadata.shared.session.title` (absent if the session has no title).
  Older plugin versions omit it -> resolver falls back to `search` (case-insensitive substring) and treats a
  single hit as the match; multiple hits w/o titles -> AmbiguousSession.
- Final reply text: see SPIKE.md "Implications". Always stream for opencode peers.
- Opencode-bound calls get a fresh `contextId` each call; key opencode sessions by `metadata.shared.session.id`.
- Stale/unknown opencode session: HTTP 200, state FAILED, `metadata.opencode.error.type == "UPSTREAM_RESOURCE_NOT_FOUND"`.
  `AskResult` gets an extra field `error_type: str | None` carrying that value.
- `list_opencode_sessions` returns `list[dict]` = `[{"id": ..., "title": str | None}]` (not just ids).
  `get_opencode_session(id)` returns `{"id", "title": str | None}`; raises `A2AError` with `code=-32001` if not found.
- Interrupt dict in AskResult: `{"request_id", "type", "phase", "details"}` from `statusUpdate.metadata.shared.interrupt`.

## a2a_client.py — protocol layer (async, httpx)

A2A 1.0 HTTP+JSON binding as served by opencode-a2a 1.3.6:
- `GET  {url}/.well-known/agent-card.json` (public)
- `POST {url}/message:send`   body `{"message": {...}, "metadata"?: {...}}` -> `{"task": {...}}` or `{"message": {...}}`
- `POST {url}/message:stream` same body, SSE response; each `data:` line is a JSON StreamResponse
  with one of: `task`, `message`, `statusUpdate`, `artifactUpdate`.
- `POST {url}/` JSON-RPC 2.0 for extension methods, e.g. `opencode.sessions.list` (params: limit, roots, search, directory) -> `result.items: Task[]` where `Task.id` == opencode session id;
  `opencode.sessions.get` (params: `session_id`) -> `result.item: {id, title, directory, ...}`.
- Message shape: `{"messageId": uuid, "role": "ROLE_USER", "parts": [{"text": "..."}], "contextId"?: str, "metadata"?: {...}}`.
- Task states: `TASK_STATE_SUBMITTED|WORKING|COMPLETED|FAILED|CANCELED|REJECTED|INPUT_REQUIRED|AUTH_REQUIRED`.
- Opencode session binding: `message.metadata = {"shared": {"session": {"id": "<opencode session id>"}}}`.
- Optional model override: `message.metadata.shared.model = {"providerID": "...", "modelID": "..."}`
  (requires `urn:opencode-a2a:extension:model-selection:v1` in `A2A-Extensions`).
- Opencode peer detection: card `capabilities.extensions[].uri` contains `urn:opencode-a2a:extension:session-binding:v1`.
- Interrupt details (opencode): status message/metadata at `metadata.shared.interrupt` = `{request_id, type, phase, details?}`.

```python
OPENCODE_SESSION_EXT = "urn:opencode-a2a:extension:session-binding:v1"

class A2AError(Exception):          # transport / HTTP / JSON-RPC errors; message is human readable
    status: int | None

@dataclass
class AgentCard:
    raw: dict
    name: str
    description: str
    streaming: bool
    extension_uris: list[str]
    skills: list[dict]              # [{id, name, description}]
    @property
    def is_opencode(self) -> bool

@dataclass
class AskResult:
    state: str                      # final TASK_STATE_*, or "TIMEOUT"
    text: str                       # concatenated text of artifacts (preferred) or final agent message
    task_id: str | None
    context_id: str | None
    interrupt: dict | None = None   # set when state == TASK_STATE_INPUT_REQUIRED / AUTH_REQUIRED
    error: str | None = None        # failure detail for FAILED/REJECTED/CANCELED/TIMEOUT

class A2AClient:
    def __init__(self, base_url: str, token: str | None, *, timeout: float = 1200.0,
                 transport: httpx.AsyncBaseTransport | None = None)   # transport for tests
    async def aclose(self) -> None
    async def __aenter__/__aexit__
    async def get_card(self) -> AgentCard
    async def ask(self, text: str, *, context_id: str | None = None,
                  opencode_session_id: str | None = None, model: str | None = None,
                  stream: bool = True, timeout: float | None = None) -> AskResult
        # stream=True uses message:stream; returns early (state INPUT_REQUIRED/AUTH_REQUIRED) with
        # `interrupt` populated as soon as such a status arrives. Overall timeout -> state "TIMEOUT".
        # stream=False uses message:send.
    async def jsonrpc(self, method: str, params: dict) -> Any     # returns `result`; raises A2AError on `error`
    async def list_opencode_sessions(self, *, search: str | None = None, limit: int = 20, roots: bool = True) -> list[str]   # session ids
    async def get_opencode_session(self, session_id: str) -> dict   # {id, title, ...}
```

## resolver.py — name -> SessionRef (uses client + state)

```python
class SessionNotFound(Exception)    # message lists known names / candidate titles
class AmbiguousSession(Exception)   # message lists matching ids

async def resolve(client: A2AClient, card: AgentCard, peer: str, name: str, *, create: bool) -> SessionRef | None
```
1. state cache hit -> return it.
2. opencode peer: list(search=name, limit=20, roots=True) -> get each -> exact title match.
   If search yields nothing, retry list without search (limit 20). 1 match -> cache + return; >1 -> AmbiguousSession; 0 -> SessionNotFound.
3. generic peer: return None if `create` (caller sends without contextId and caches the returned contextId), else SessionNotFound.

## mcp_server.py — MCP tools (MCPServer, stdio)

- `list_peers()` -> per peer: name, url, reachable, agent name, skills, is_opencode.
- `list_sessions(peer)` -> cached names + (opencode) titled root sessions (id, title).
- `ask_peer(peer, session, message, timeout_seconds=1200, model=None)` -> reply text, or a clear multi-line
  explanation for INPUT_REQUIRED (peer, session, interrupt type, details, request id,
  "approve it in that session, then ask again"), FAILED, TIMEOUT, unknown session.
  `model` is optional `provider/modelID` (e.g. `llmgw/grok-4.6`); sent as `metadata.shared.model`.
  On a cached ref that FAILS, invalidate the cache entry and resolve again once.
  Generic peers: unknown name -> new context, cache returned contextId.
- `forget_session(peer, session)`.
Every tool description: "Only call when the user explicitly asks to contact another agent/session."

## launcher.py — `a2a-peers` CLI
`launch --name N --workspace DIR [--opencode-port P] [--a2a-port Q]`, `list`, `kill NAME`, `check NAME`.
Starts `opencode-a2a serve` in background with env:
`A2A_STATIC_AUTH_CREDENTIALS=[{"scheme":"bearer","token":T,"principal":N}]`, `OPENCODE_BASE_URL=http://127.0.0.1:P`,
`A2A_TASK_STORE_DATABASE_URL=sqlite+aiosqlite:///<run_dir>/N.db`, `A2A_HOST=127.0.0.1`, `A2A_PORT=Q`,
`A2A_PUBLIC_URL=http://127.0.0.1:Q`, `OPENCODE_WORKSPACE_ROOT=DIR`. Logs/pids in `run_dir()`.
Also starts `opencode --port P` in the workspace (unless that port is already listening), waits for
`GET /global/health`, then `POST /session` with `{"title": N}` if no session of that title exists.
`kill` stops both the opencode TUI and opencode-a2a (pid files `N.pid` and `N.opencode.pid`).
