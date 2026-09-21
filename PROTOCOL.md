# Local Hands Protocol v1

Wire protocol between the LLM (in the browser tab), the extension
(`extension/content.js` + `extension/background.js`), and the bridge
(`bridge.py` at `http://127.0.0.1:8787`).

## 1. Chat-level sentinels (extension side)

Blocks are plain text inside a completed **assistant** message. The extension
parses only the latest completed assistant turn, never user messages, and never
while the model is generating.

### Handshake (popup "Initialize current chat")

The extension types and sends exactly this user message into the chat:

```
[LOCAL_HANDS_V1_READY]
Local Hands is connected to this chat.
Bridge tools (executed on this PC by the local bridge): fs.read, fs.list, fs.stat, fs.write, fs.patch, fs.mkdir, fs.move, fs.copy, fs.delete, shell.powershell, shell.cmd, process.list, process.start, process.kill, file.tail.
Browser tools (executed by this extension only): browser.status, browser.tabs, browser.open, browser.navigate, browser.read_page.
Protocol: to call one tool, reply with exactly one block:
[[LOCAL_HANDS_V1:CALL]]
{"id":"<unique-id>","tool":"<tool>","args":{...}}
[[/LOCAL_HANDS_V1:CALL]]
To batch multiple tools in one reply:
[[LOCAL_HANDS_V1:BATCH]]
{"calls":[{"id":"...","tool":"...","args":{...}}, ...]}
[[/LOCAL_HANDS_V1:BATCH]]
Your next user message will contain the execution outcome as a [[LOCAL_HANDS_V1:RESULT]] block (single call) or a [[LOCAL_HANDS_V1:RESULTS]] block (batch).
```

### CALL block (single tool)

```
[[LOCAL_HANDS_V1:CALL]]
{"id":"a1","tool":"fs.read","args":{"path":"C:\\Users\\YOUR_NAME\\notes.txt"}}
[[/LOCAL_HANDS_V1:CALL]]
```

### BATCH block (multiple tools, executed in order)

```
[[LOCAL_HANDS_V1:BATCH]]
{"calls":[{"id":"b1","tool":"process.list","args":{}},{"id":"b2","tool":"file.tail","args":{"path":"C:\\Users\\YOUR_NAME\\app.log","lines":50}}]}
[[/LOCAL_HANDS_V1:BATCH]]
```

`id` is a caller-chosen unique string. Duplicate ids are **never**
re-executed: the bridge keeps an in-memory LRU (512 entries) and the extension
keeps `chrome.storage.local` records; a repeat returns the cached result
(flagged `"cached": true`).

### RESULT reply (single call)

The extension types and sends:

```
[[LOCAL_HANDS_V1:RESULT]]
[{"id":"a1","tool":"fs.read","status":"ok","result":{...}}]
[[/LOCAL_HANDS_V1:RESULT]]
```

Each item is exactly the per-call result object (section 3). On a batch:

```
[[LOCAL_HANDS_V1:RESULTS]]
{"results":[{"kind":"BATCH","status":"ok","results":[{"id":"b1","tool":"process.list","status":"ok","result":{...}},{"id":"b2","tool":"file.tail","status":"error","error":{"code":"NOT_FOUND","message":"..."}}]}]}
[[/LOCAL_HANDS_V1:RESULTS]]
```

If execution was blocked (paused/stopped), the reply contains a single
`status:"skipped"` item with `error.code` `NOT_EXECUTED_*`.

## 2. Bridge HTTP (extension ↔ bridge)

- Base: `http://127.0.0.1:8787` (loopback only). The bridge is intentionally
  unauthenticated: it binds to `127.0.0.1` only, so no auth token is required
  or sent. Endpoints below need no `Authorization` / `X-Local-Hands-Token`
  header.

| Endpoint | Method | Purpose |
|---|---|---|
| `/health` | GET | `{"status":"ok","protocol":"LOCAL_HANDS_V1","version":1,"pid":<pid>}` |
| `/capabilities` | GET | `{"protocol":"LOCAL_HANDS_V1","version":1,"mode":"<mode>","tools":[...],"browser_tools":[...]}` |
| `/invoke` | POST | execute one tool |
| `/batch` | POST | execute an ordered list of tools |

### POST /invoke request

```json
{"id":"a1","tool":"fs.read","args":{"path":"C:\\Users\\YOUR_NAME\\notes.txt"}}
```

HTTP 200 → result envelope (section 3). HTTP 400 → `{"error":{"code":"...","message":"..."}}`
(includes malformed JSON: `{"error":"bad_json"}`).

### POST /batch request

```json
{"calls":[{"id":"b1","tool":"process.list","args":{}}, ...]}
```

HTTP 200 → `{"results":[ <envelope per call> ]}` in request order.

## 3. Per-call result envelope

```json
{
  "id": "a1",
  "tool": "fs.read",
  "status": "ok",
  "result": { },
  "elapsed_ms": 12,
  "cached": false
}
```

On failure: `status:"error"`, `result` absent, and

```json
"error": { "code": "FIND_NOT_FOUND", "message": "..." }
```

Error codes include: `BAD_JSON`, `UNKNOWN_TOOL`, `BAD_ARGS`, `PATH_OUTSIDE_ALLOWED_ROOTS`,
`WRITE_DENIED_IN_SAFE_MODE`, `SAFE_MODE_DENIED`, `TIMEOUT`, `NOT_FOUND`,
`FIND_NOT_FOUND`, `NO_ACTIVE_TAB`, `UNSUPPORTED_PAGE`, `INTERNAL_ERROR`.

## 4. Tool reference (exact args)

### fs.* (bridge)

| tool | args | result |
|---|---|---|
| `fs.read` | `{"path": str}` | `{"path","size_bytes","content"}` |
| `fs.list` | `{"path": str, "recursive"?: bool}` | `{"path","entries":[{"name","is_dir","size_bytes"}]}` |
| `fs.stat` | `{"path": str}` | `{"path","exists","is_dir","is_file","size_bytes","modified_ms"}` |
| `fs.write` | `{"path": str, "content": str}` | `{"path","written_bytes"}` |
| `fs.patch` | `{"path": str, "find": str, "replace": str}` — deterministic single replace; fails `FIND_NOT_FOUND` if `find` is absent | `{"path","replacements":1}` |
| `fs.mkdir` | `{"path": str}` | `{"path"}` |
| `fs.move` | `{"src": str, "dst": str}` | `{"src","dst"}` |
| `fs.copy` | `{"src": str, "dst": str}` | `{"src","dst"}` |
| `fs.delete` | `{"path": str}` | `{"path","deleted"}` |

### shell.* (bridge; require `shell_enabled`)

| tool | args | result |
|---|---|---|
| `shell.powershell` | `{"command": str, "timeout_seconds"?: int}` | `{"exit_code","stdout","stderr","timed_out"}` |
| `shell.cmd` | `{"command": str, "timeout_seconds"?: int}` | `{"exit_code","stdout","stderr","timed_out"}` |

### process.* (bridge; mutations require `process_control`)

| tool | args | result |
|---|---|---|
| `process.list` | `{}` or `{"filter"?: str}` | `{"processes":[{"pid","name","cpu_ms","mem_bytes"}]}` |
| `process.start` | `{"command": str, "args"?: [str], "cwd"?: str}` | `{"pid","command"}` |
| `process.kill` | `{"pid": int}` | `{"pid","killed"}` |

### file.tail (bridge)

| tool | args | result |
|---|---|---|
| `file.tail` | `{"path": str, "lines"?: int (default 50, max 1000)}` | `{"path","lines":[str]}` |

### browser.* (extension only — never reach the bridge)

| tool | args | result |
|---|---|---|
| `browser.status` | `{}` | `{ok, bridge:{host,port}, enabled_chats, paused, stopped}` |
| `browser.tabs` | `{}` | `[{id,url,title,active}]` |
| `browser.open` | `{"url": str}` | `{id,url}` |
| `browser.navigate` | `{"tabId": int, "url": str}` | `{id,url}` |
| `browser.read_page` | `{"tabId"?: int}` (default active tab) | `{tabId,url,title,bodyText,truncated,total_chars}` — `bodyText` capped at 20000 chars; protected pages fail `UNSUPPORTED_PAGE` |

## 5. Output truncation

Any inline string field larger than `max_inline_bytes` (config, default
32768) is replaced with:

```json
{"truncated": true, "total_bytes": 123456, "head": "...", "tail": "...", "full_output_path": "C:\\Users\\YOUR_NAME\\Desktop\\chatgpt-local-hands\\output\\<sha256-prefix>.txt"}
```

## 6. Safety rules (enforced, not advisory)

- Bridge binds to 127.0.0.1 only; `main()` refuses any other host in config.
- Modes: `safe` (read-only, no writes/shell/process mutations),
  `workspace_full_access` (fs inside `allowed_roots`, shell/process only if
  flagged), `full_pc_access` (fs unrestricted).
- Path checks resolve canonical paths (symlinks/junctions) and reject escape
  from `allowed_roots`.
- Extension: assistant turns only; no execution while generating; no execution
  of user-message blocks; DOM failure ⇒ PAUSE with a visible DOM ERROR, no
  blind retries; Emergency STOP pauses and clears the queue without deleting
  the bridge or config.
