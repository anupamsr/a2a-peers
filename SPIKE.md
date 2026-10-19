# SPIKE: opencode-a2a 1.3.6 protocol behaviour (empirical)

Setup: `opencode serve` 1.18.10 on 127.0.0.1:4191, `opencode-a2a serve` on 127.0.0.1:8191 (env as in CONTRACTS.md launcher),
scratch `/var/folders/.../T/opencode/spike`, `opencode.json` = `{"model":"llmgw/gpt-5-mini","permission":{"bash":"ask","edit":"ask"}}`.
Token `spiketoken`. Raw captures: `q*.txt/json` in the scratch dir.

## 0. Big finding: extension negotiation headers are MANDATORY (CONTRACTS.md omits this)

Every opencode-specific feature gets rejected unless you list its extension URI in the `A2A-Extensions` request header (comma-separated):

| Feature | Required `A2A-Extensions` value |
|---|---|
| `message.metadata.shared.session.id` on message:send/stream | `urn:opencode-a2a:extension:session-binding:v1` |
| `opencode.sessions.list` / `.get` / `.messages.list` | `urn:opencode-a2a:extension:session-management:v1` (NOT in the card's `capabilities.extensions`) |
| `a2a.interrupt.permission.reply` / `question.reply` / `question.reject` | `urn:opencode-a2a:extension:interactive-interrupt:v1` |
| `opencode.permissions.list` / `opencode.questions.list` | `urn:opencode-a2a:extension:interrupt-recovery:v1` |
| `block_type` + progress metadata in SSE | `urn:opencode-a2a:extension:stream-hints:v1` (optional) |

Without the header, REST returns HTTP 400:
```json
{"error":{"code":400,"status":"FAILED_PRECONDITION","message":"Request requires explicit A2A extension negotiation via A2A-Extensions.",
  "details":[{"reason":"UNSUPPORTED_OPERATION","metadata":{"type":"EXTENSION_NEGOTIATION_REQUIRED","requiredExtensions":"[\"urn:...session-binding:v1\"]",...}}, ...]}}
```
JSON-RPC returns `error.code=-32004`, `"Method X requires explicit A2A extension negotiation via the A2A-Extensions header."`.
Also: `GET /tasks/{id}` requires the `A2A-Version: 1.0` header (otherwise 400 "A2A version '0.3' is not supported"). message:send/stream and JSON-RPC worked without it, but sending `A2A-Version: 1.0` on every request is safest.
The card advertises: `session-binding`, `model-selection`, `stream-hints`, `interactive-interrupt` (it does not list session-management or interrupt-recovery).

## Q1. Bind to a pre-created opencode session — YES

```
curl -X POST "http://127.0.0.1:4191/session?directory=$S" -d '{"title":"spike-alpha"}'
 -> {"id":"ses_ed00bffd4ffeGqeKdel1p9vbZK","title":"spike-alpha","directory":"/private/var/...",...}
curl -X POST :8191/message:send -H 'authorization: Bearer spiketoken' -H 'A2A-Extensions: urn:opencode-a2a:extension:session-binding:v1' \
 -d '{"message":{"messageId":"m1","role":"ROLE_USER","parts":[{"text":"Reply with just the word PINEAPPLE"}],
      "metadata":{"shared":{"session":{"id":"ses_ed00..."}}}}}'
```
Response (13.7s on grok-4.6, ~2s on gpt-5-mini):
```json
{"task":{"id":"bdf4...","contextId":"52288a13-...",
  "status":{"state":"TASK_STATE_COMPLETED","message":{"role":"ROLE_AGENT","parts":[{"text":"Completed."}]}},
  "artifacts":[{"artifactId":"7c62...","name":"response","parts":[{"text":"PINEAPPLE"}]}],
  "history":[<user msg>],
  "metadata":{"shared":{"session":{"id":"ses_ed00..."}}}}}
```
- `GET :4191/session/<id>/message` shows user "Reply with just the word PINEAPPLE" + assistant "PINEAPPLE" in **that** session. No new session was created.
- First bind accepted. The second message to the same id (MANGO) was also accepted and appended to the same session. Ownership is keyed to the auth **principal** (`session_manager.py:113-127`: the first caller becomes owner and other principals get rejected), so one token per client is fine.
- `task.contextId` is a **fresh random UUID on every call** even for the same bound session (52288a13…, then 4799e514…). Don't use contextId to identify an opencode session; the stable key is `task.metadata.shared.session.id`.
- `status.message` text is a placeholder ("Completed."). The real reply is in `artifacts[].parts[].text`.

## Q2. sessions.list / sessions.get

Header `A2A-Extensions: urn:opencode-a2a:extension:session-management:v1` is required.

- **`"roots": true` is BROKEN**: the plugin turns the bool into the string `"True"` (`opencode_upstream_client.py:410`, `str(value)`), and opencode returns 400 on `roots=True` (it only accepts `true`/`false`). Result: `{"error":{"code":-32003,"message":"Upstream OpenCode error","data":[...{"upstreamStatus":"400"}]}}`. `roots:false` and `roots:"true"` fail the same way. **Omit `roots`.**
- `{"limit":20}` works:
```json
{"jsonrpc":"2.0","id":1,"result":{"items":[
  {"id":"ses_ed00bffd...","contextId":"ctx:opencode-session:ses_ed00bffd...","status":{"state":"TASK_STATE_COMPLETED"},
   "metadata":{"shared":{"session":{"id":"ses_ed00bffd..."}}}}, ...]}}
```
  The items carry **no title**.
- `search` filters by **title, case-insensitive substring**. With sessions spike-alpha and spike-beta: `spike-alpha`→[alpha], `alpha`→[alpha], `ALPHA`→[alpha], `spike`→[beta, alpha], `quiet-cabin` (slug)→[], `PINEAPPLE` (content)→[], `nomatch`→[].
- `opencode.sessions.get {"session_id":id}` returns the **same Task shape and still no title**:
  `{"result":{"item":{"id","contextId":"ctx:opencode-session:<id>","status":{"state":"TASK_STATE_COMPLETED"},"metadata":{"shared":{"session":{"id"}}}}}}`.
  Unknown id returns `{"error":{"code":-32001,"message":"Session not found"}}`.
  CONTRACTS.md's `result.item: {id,title,directory,...}` is **wrong**. The `title` field only exists in the plugin's `_normalize_session_summary`, which is used for fork/share/summarize/revert, not for get.
- The A2A layer can't do an exact title match. The resolver can use `search=name` as a (substring) candidate filter, but to confirm the title it has to read opencode directly (`GET {opencode}/session/<id>` → `.title`, or `GET {opencode}/session?search=` → `[].title`). That works for launcher-created peers (`opencode_port` known). For other peers, treat a single search hit as the match, and if there are several, rank exact-name/prefix hits first or report the session as ambiguous.

## Q3. Streaming SSE format

`POST /message:stream` with the same body plus `-N`. The wire format is `data: <json>\n\n` only: **no `event:` lines**, and the keepalive is the comment `: ping - <ts>` every 15s. Each data JSON has exactly one top-level key:
1. `{"task":{id,contextId,status:{state:"TASK_STATE_WORKING"},history}}`, first event
2. `{"artifactUpdate":{taskId,contextId,artifact:{artifactId,parts,metadata?},append?,lastChunk?}}`
3. `{"statusUpdate":{taskId,contextId,status:{state,message?},metadata?}}`
4. The stream closes after a terminal or interrupted `statusUpdate`. No `final` flag appears.

artifactId patterns (always prefixed by `<taskId>:`):
- `<task>:stream:text`: assistant text. The first chunk has no `append`; later chunks have `"append":true` (deltas). It ends with **one chunk `"lastChunk":true` and no `append`, which carries the FULL final text of the LAST assistant message** (in a tool-using turn, only the post-tool answer, e.g. `"1: beta notes"`, not the preamble "I'll read b.txt now."). That chunk **replaces**; it does not append.
- `<task>:stream:reasoning`: reasoning text (from source, `stream_runtime.py:255`; gpt-5-mini didn't emit any).
- `<task>:stream:tool:<partId>`: tool calls, `parts:[{"data":{call_id,tool,status:pending|running|completed,input,output?,title?}}]`, i.e. **data parts, not text**.
- `<task>:error`: error text with `lastChunk:true` (see Q6).

With `stream-hints` negotiated: `artifact.metadata.shared.stream.block_type` ∈ `text|reasoning|tool_call`, plus `message_id`, `sequence`, `event_id`. Working `statusUpdate`s carry `metadata.shared.progress.type` = `step-start|step-finish`, and the final COMPLETED status carries `metadata.shared.usage`. Without the hint, `block_type` is absent, but the artifactId suffix still tells the blocks apart.
Final event: `{"statusUpdate":{...,"status":{"state":"TASK_STATE_COMPLETED"},"metadata":{"shared":{"session":{"id":...}}}}}` with no status.message.

## Q4. Concurrency on one bound session — both succeed, serialized

Two message:send calls in the background at the same moment (gpt-5-mini) both came back HTTP 200 COMPLETED: APPLE after 1.93s, BANANA after 3.98s. The opencode transcript shows them strictly in order (APPLE turn, then BANANA turn). There was no error or busy rejection: the second call just waits.
Caveat seen with a slow model (grok-4.6): queued calls wait behind the running turn, and the plugin's own upstream timeout (`OPENCODE_TIMEOUT`, default **120s**, a2a.log "OpenCode request timed out") can expire for message:send. Streaming uses `OPENCODE_TIMEOUT_STREAM`, default 900s.

## Q5. Permission → INPUT_REQUIRED

**Streaming** (headers: session-binding + interactive-interrupt), prompt "Run the bash command: echo hi":
```
data: {"artifactUpdate":{... "artifactId":"<task>:stream:tool:prt_...","parts":[{"data":{"tool":"bash","status":"running","input":{"command":"echo hi"}}}]}}
data: {"statusUpdate":{"taskId":"379bf522-...","contextId":"668a76fb-...",
  "status":{"state":"TASK_STATE_INPUT_REQUIRED"},
  "metadata":{"shared":{"session":{"id":"ses_ecffdf456ffe7nJybOJkup472R"},
    "interrupt":{"request_id":"per_13002edb5001QItu4xQHK8bynF","type":"permission","phase":"asked",
                 "details":{"permission":"bash","patterns":["echo hi"]}}}}}}
```
- The interrupt lives in **`statusUpdate.metadata.shared.interrupt`**. `status.message` is absent.
- **The stream closes immediately** after INPUT_REQUIRED (curl exit 0, long before the timeout).
- After the client disconnects, the opencode turn **stays pending**: `/session/status` shows busy, `GET :4191/permission` lists `per_1300…`, and `GET /tasks/<id>` gives `TASK_STATE_INPUT_REQUIRED`. Approving it in the opencode TUI (or replying via RPC) resumes it.
- `opencode.permissions.list` (interrupt-recovery ext) gives `{"result":{"items":[{"request_id","session_id","interrupt_type":"permission","task_id","context_id","details":{...},"expires_at":<epoch>}]}}`.
- `a2a.interrupt.permission.reply` params: `request_id` (req), `reply` ∈ `once|always|reject` (req), `message?`, `metadata?`. The call `{"request_id":"per_1300...","reply":"reject"}` returned `{"result":{"ok":true,"request_id":"per_1300..."}}`. The bash tool ended with `error`, and the session went idle. The A2A task stayed `INPUT_REQUIRED` in the store (it isn't updated after the stream is gone).
- While the session is blocked, a **message:send to that session just hangs** (it queues behind the pending turn). It hit `-m30` with no response. After the reject, the queued message ran ("QUEUED" appeared in the transcript).
- **Non-streaming message:send that itself triggers the permission**: it **blocks until the client timeout** (`-m60`, HTTP 000) and never returns INPUT_REQUIRED. Worse, that permission is **not tracked** by the plugin: it is missing from `opencode.permissions.list`, and `a2a.interrupt.permission.reply` returns `{"code":-32004,"message":"Interrupt request not found"}`. I had to clear it directly with `POST :4191/permission/<id>/reply {"reply":"reject"}`. So **always use streaming** for asks.

## Q6. Unknown session id

message:send → **HTTP 200**, task FAILED:
```json
{"task":{"id":"...","contextId":"...","status":{"state":"TASK_STATE_FAILED","message":{"role":"ROLE_AGENT","parts":[{"text":
 "OpenCode rejected the request because the target resource was not found (send_message, status=404, detail={\"name\":\"NotFoundError\",\"data\":{\"message\":\"Session not found: ses_doesnotexist\"}})."}]}},
 "metadata":{"opencode":{"error":{"upstream_status":404.0,"type":"UPSTREAM_RESOURCE_NOT_FOUND"}}}}}
```
The stream sends `task`(WORKING), then `artifactUpdate` with artifactId `<task>:error`, `parts[0].text` = same message, `lastChunk:true`, then `statusUpdate` FAILED with `metadata.opencode.error.{upstream_status:404.0, type:"UPSTREAM_RESOURCE_NOT_FOUND"}` (no status.message).

## Q7. contextId continuation without session metadata — YES

1st send (no metadata, no extension header) gave `task.contextId="330b6338-..."`, `task.metadata=null`, and **one new opencode session** with an auto title from the prompt.
2nd send with `message.contextId="330b6338-..."` came back with the same contextId and answer "ZEBRA" (it remembered), and **no new session** was created. The transcript of the one session holds all 4 messages. That session's id never appears in the response (`metadata` is null).

## Implications for a2a_client.py

- **Headers on every request**: `Authorization: Bearer <t>`, `A2A-Version: 1.0`, `A2A-Extensions: <comma list>`. For opencode peers send all of: `urn:opencode-a2a:extension:session-binding:v1, urn:opencode-a2a:extension:session-management:v1, urn:opencode-a2a:extension:interactive-interrupt:v1, urn:opencode-a2a:extension:interrupt-recovery:v1, urn:opencode-a2a:extension:stream-hints:v1`. Sending them to a generic peer should be harmless, but only send them when `card.is_opencode`.
- **list_opencode_sessions**: do NOT send `roots` (plugin bug, upstream 400). Use `{"limit":N,"search":name}`; ids at `result.items[].id` (also `metadata.shared.session.id`).
- **get_opencode_session**: returns no title (`result.item = {id, contextId, status, metadata}`). Exact title matching needs opencode direct (`GET http://127.0.0.1:<opencode_port>/session/<id>` → `.title`) when `opencode_port` is known. Otherwise rely on `search` (case-insensitive title substring).
- **Final text**:
  - send: join `task.artifacts[*].parts[*].text` (skip artifacts whose artifactId ends `:error`). Ignore `status.message` on COMPLETED (it's the "Completed." placeholder).
  - stream: per artifactId (only those ending `:stream:text`, or `block_type=="text"`), buffer = `parts.text` if `append` is falsy, else `buffer += parts.text`. The `lastChunk:true` chunk (no append) replaces the buffer with the final message text. Ignore `:stream:reasoning`, `:stream:tool:*` (data parts), and treat `:error` as error text.
- **Context id**: `task.contextId` / `statusUpdate.contextId`. Useful for generic peers only. For opencode-bound calls it changes every call, so key by `metadata.shared.session.id`.
- **Interrupt**: from `statusUpdate.metadata.shared.interrupt` → `request_id`, `type` (permission|question), `phase`, `details` (`{permission, patterns}` for permission). As fallback, also check `status.message.metadata.shared.interrupt`. Return as soon as INPUT_REQUIRED/AUTH_REQUIRED arrives (the server closes the stream anyway). The turn stays pending in opencode, so "approve it in that session" is accurate. The request can also be answered with `a2a.interrupt.permission.reply {request_id, reply: once|always|reject}`.
- **Error message**: on FAILED, prefer `status.message.parts[*].text` (send), else the `:error` artifact text (stream). Add `metadata.opencode.error.type` / `upstream_status` (in task.metadata or statusUpdate.metadata). On HTTP non-2xx, use `error.message` (+ `error.details[*].metadata.type`). On JSON-RPC, use `error.message` (+ code).
- **SSE parsing rules**: split on blank lines. Ignore lines starting with `:` (pings). There's no `event:` field, so dispatch on the single top-level key of the `data:` JSON (`task|message|statusUpdate|artifactUpdate`); accept multiple `data:` lines per event by joining them with `\n`. Terminal states: COMPLETED/FAILED/CANCELED/REJECTED. Interrupted: INPUT_REQUIRED/AUTH_REQUIRED. If the stream EOFs with no terminal status, fall back to `GET /tasks/{taskId}` (with `A2A-Version: 1.0`).
- **Always stream for opencode peers.** Non-streaming send blocks indefinitely on permission prompts and leaves an interrupt the plugin doesn't track (can't be replied to via A2A). It is also capped by the plugin's 120s `OPENCODE_TIMEOUT`.
- **Concurrent asks to one session are serialized server-side** (no error). The client timeout must cover the queue wait. If a session is blocked on a permission, new asks hang, so the client timeout → `TIMEOUT` result with a hint that the session may be awaiting approval.
- **Unknown/stale session id comes back as HTTP 200 + FAILED** with `metadata.opencode.error.type == "UPSTREAM_RESOURCE_NOT_FOUND"`. Use that to invalidate the cache and re-resolve.
- Launcher: consider setting `OPENCODE_TIMEOUT` higher, and note the session-management ext isn't in the card, so `is_opencode` should keep keying on session-binding.
