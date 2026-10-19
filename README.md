# a2a-peers (moved)

Canonical source is now [`../opencode-a2a`](../opencode-a2a), under
`src/a2a_peers` and [`docs/a2a-peers/README.md`](../opencode-a2a/docs/a2a-peers/README.md).

```bash
uv tool install --force --editable --with '.[peers]' ../opencode-a2a
```

The rest of this file is the pre-move snapshot and is no longer authoritative.

---

Let a coding agent (opencode, Cursor, anything that speaks MCP) talk to other agents over the
[A2A protocol](https://a2a-protocol.org), addressing them by **peer name** and **session name**.

Typical use: you have several interactive opencode sessions, each researching or implementing
something different. From any of them you can say *"ask peer `api`, session `auth-research`, how the
token refresh works"* and get the answer back, while the exchange shows up live in that other session.

```
 opencode / Cursor ──MCP(stdio)──> a2a-peers-mcp ──A2A──> opencode-a2a ──> opencode (TUI you work in)
                                                  └──A2A──> any other A2A agent
```

## Components

| Piece | What it does |
|---|---|
| `a2a-peers-mcp` | MCP server (stdio). Tools: `list_peers`, `list_sessions`, `ask_peer`, `forget_session`. |
| `a2a-peers` | CLI that starts an [`opencode-a2a`](https://github.com/Intelligent-Internet/opencode-a2a) front-end for an opencode TUI and registers it as a peer. Also registers external A2A agents. |
| `~/.config/a2a-peers/peers.json` | Peer registry (URL + bearer token), mode 0600. |
| `~/.local/share/a2a-peers/` | Session-name cache (`state.json`) and launcher logs/pids (`run/`). |

## Install

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```bash
uv tool install /path/to/this/repo            # puts a2a-peers and a2a-peers-mcp on PATH
uv tool install opencode-a2a                  # only needed to expose opencode sessions as peers
```

> Session titles in `list_sessions` and exact title matching need an opencode-a2a build that
> includes `metadata.shared.session.title` in `opencode.sessions.list` (patched locally; see
> "opencode-a2a patch" below). Older builds still work but fall back to substring search.

## Expose an opencode session as a peer

```bash
a2a-peers launch --name api --workspace ~/src/my-api
# Started opencode-a2a for 'api' ...
# Now start the opencode TUI for this peer (peer calls fail until it is running):
#   cd ~/src/my-api && opencode --port 41234
```

Run the printed command. That TUI *is* the peer: A2A messages land in its sessions and you can
watch and interact with them. Sessions are addressed by **exact session title**, so give the sessions
you want reachable a distinct title (rename it in the TUI's session list, or set it via the opencode
server API: `PATCH /session/<id>` with `{"title": "..."}`). Use `list_sessions` to see current titles.

Other commands:

```bash
a2a-peers list                 # peers, ports, process status
a2a-peers check api            # card, auth (expects 401 without token), opencode reachable
a2a-peers kill api [--forget]  # stop opencode-a2a (and remove from registry)
a2a-peers add other --url https://agent.example.com [--token T]   # register a non-opencode A2A agent
```

One bearer token per peer is generated and stored in the registry; re-launching reuses it.

## Register the MCP server

### opencode (`~/.config/opencode/opencode.json`)

```json
{
  "$schema": "https://opencode.ai/config.json",
  "mcp": {
    "a2a-peers": { "type": "local", "command": ["a2a-peers-mcp"], "enabled": true }
  }
}
```

### Cursor (`~/.cursor/mcp.json` or `.cursor/mcp.json`)

```json
{
  "mcpServers": {
    "a2a-peers": { "type": "stdio", "command": "a2a-peers-mcp" }
  }
}
```

If `a2a-peers-mcp` isn't on Cursor's PATH, use the absolute path (`which a2a-peers-mcp`).

## Using it

Tools only run when you explicitly ask:

- *"list a2a peers"* → `list_peers`
- *"what sessions does peer api have?"* → `list_sessions`
- *"ask peer api, session auth-research: where is the refresh token rotated?"* → `ask_peer`
- *"forget session auth-research on api"* → `forget_session` (drops the cached name → id mapping)

Behaviour:

- **opencode peers**: the session name must match an existing session title exactly. Unknown or
  ambiguous names return an error listing what exists. Nothing is created silently.
- **Other A2A agents**: the first `ask_peer` with a new session name starts a new conversation
  (A2A `contextId`), cached under that name for follow-ups.
- **Approval prompts**: if the peer needs a permission/question answered, `ask_peer` returns
  immediately with *"waiting for permission approval: bash: … (request id …)"*. Approve it in that
  TUI, then ask again.
- **Busy sessions**: asks to the same session queue behind the running turn. If it doesn't finish
  within `timeout_seconds` (default 1200) you get a timeout message.
- Session name → id lookups are cached; stale entries (deleted session) are re-resolved once automatically.

## Sharing with a team

Each person runs their own MCP server and registry. To let teammates reach your peer, it must be
reachable from their machine (the launcher binds `127.0.0.1`), and you share the peer's URL and token
with them, who add it with `a2a-peers add`. All callers of one peer should use the **same token**:
opencode-a2a ties a session to the first caller's identity and rejects others.

Tokens are stored in plaintext in `peers.json` (0600). Don't expose peers beyond localhost without TLS.

## opencode-a2a patch

Two small changes were made to opencode-a2a (local clone, installed with
`uv tool install --force --editable <clone>`):

1. Boolean query params were sent as `True`/`False`, which opencode rejects (`roots=true` filter broke).
2. `opencode.sessions.list` / `.get` items now carry `metadata.shared.session.title`.

## Development

```bash
uv sync
uv run pytest -q
uvx ruff check src tests && uvx ruff format --check src tests
```

`CONTRACTS.md` describes module interfaces; `SPIKE.md` records the empirically verified
opencode-a2a protocol behaviour the client relies on.
