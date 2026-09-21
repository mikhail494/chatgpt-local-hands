# Local Hands for ChatGPT

Run local tools from regular **ChatGPT in your browser** (no API, no Codex, no
credits). The LLM stays in the browser tab. This project adds a local
`127.0.0.1:8787` HTTP bridge (Python stdlib) and a Chrome extension that
watches completed assistant messages on `chatgpt.com`, executes the requested
tools, and types the result back into the chat.

```
C:\Users\MG\Desktop\chatgpt-local-hands\
  bridge.py                  the whole bridge server (stdlib only)
  config.json                mode, allowed_roots, shell/process switches
  output\                    full-output spill files for oversized results
  bridge.log                 bridge log
  bridge.pid                 pid of the running bridge
  tests\test_bridge.py       auto-tests A-K
  START_LOCAL_HANDS.bat      start (prevents double launch), leaves it running
  STOP_LOCAL_HANDS.bat       stop only the PID from bridge.pid
  STATUS_LOCAL_HANDS.bat     RUNNING / DEGRADED / STOPPED + /health
  extension\                 Chrome MV3 extension (load unpacked)
    manifest.json
    background.js            service worker: bridge HTTP, browser.* tools, dedupe
    content.js               chatgpt.com only: DOM watch, composer, send
    popup.html/js/css        status, address, mode, pause, initialize, emergency stop
  PROTOCOL.md                exact tool JSON schema and message protocol
```

## How it works

1. The bridge listens on `http://127.0.0.1:8787` (loopback only, never a LAN
   or wildcard address). It is intentionally localhost-only with no
   authentication: any process already running locally on this PC can call it.
2. On `chatgpt.com`, the content script watches assistant turns. It never acts
   while the model is still generating (no visible Stop button **and** text
   stable for 1 s). It parses only the latest completed assistant message and
   **never** executes blocks from user messages.
3. The extension's service worker executes `fs.*`, `shell.*`, `process.*`,
   `file.tail` by calling the bridge over localhost HTTP, and executes
   `browser.*` itself through `chrome.tabs` / `chrome.scripting`. Python never
   controls Chrome, and there is no WebSocket, CDP, long polling, or second
   browser.
4. The result is typed into the chat composer as a `[[LOCAL_HANDS_V1:RESULT]]`
   (or `[[LOCAL_HANDS_V1:RESULTS]]` for batches) message and sent.
5. Request ids are deduped in both the bridge (in-memory LRU) and the extension
   (`chrome.storage.local`): a repeated id returns the cached result and is
   never re-executed.

## Setup (the only manual steps)

1. **Start the bridge** (leave it running):

   ```
   START_LOCAL_HANDS.bat
   ```

   Check with `STATUS_LOCAL_HANDS.bat`.

2. **Load the extension** (one time):
   - Open `chrome://extensions`
   - Turn **Developer mode** ON
   - **Load unpacked** → pick `C:\Users\MG\Desktop\chatgpt-local-hands\extension`

   No token is needed — the bridge is localhost-only and unauthenticated.
3. **Enable a chat**: open a ChatGPT chat and click **Initialize current chat**
   in the popup. The extension sends a `[LOCAL_HANDS_V1_READY]` message into
   that chat listing the available tools.

Everything else is automatic for that chat.

## Emergency STOP

The popup's **Emergency STOP** disables processing for all chats, clears the
pending extension queue, and marks the state paused. It does **not** stop the
bridge or touch `config.json`. Click **Resume** (or
re-Initialize) to continue. To stop the bridge itself: `STOP_LOCAL_HANDS.bat`.

## Modes (`config.json`)

| mode | filesystem | shell / process |
|---|---|---|
| `safe` | read-only inside `allowed_roots` | disabled (writes/shell/process mutations rejected) |
| `workspace_full_access` | full read/write inside `allowed_roots` | only if `shell_enabled` / `process_control` are true |
| `full_pc_access` | unrestricted | only if the flags are true |

Path checks use resolved canonical paths (symlinks/junctions are resolved), so
`../` or a link cannot escape `allowed_roots`. Restart the bridge after
editing `config.json`.

## Security notes

- The bridge binds strictly to `127.0.0.1`; remote machines cannot reach it.
- The bridge is intentionally unauthenticated: any process already running
  locally on this PC could call it, so keep `mode` restrictive.
- The extension talks to the bridge from the service worker with a fixed
  `http://127.0.0.1:8787` host permission; the content script is injected only
  into `https://chatgpt.com/*`.
- There is no CORS wildcard and no browser-facing endpoint on the bridge.
- Treat "the model decides what runs on your PC" as the threat model: keep
  `mode` as restrictive as your workflow allows.

## Tests

```
python tests\test_bridge.py
```

Runs A-K against a real bridge instance (bind check, no-token operation,
fs, shell, process.list, batch, idempotency, malformed JSON, path traversal,
restart without any token). Exits 0 only when all pass.

## Troubleshooting

- **Popup says Disconnected** — run `STATUS_LOCAL_HANDS.bat`; if STOPPED, run
  `START_LOCAL_HANDS.bat`. Check `bridge.log` / `bridge.out`.
- **401 from the bridge / UNAUTHORIZED in chat** — no token is used; if you see
  this it is stale. Verify the bridge answers `STATUS_LOCAL_HANDS.bat` and reload
  the extension at `chrome://extensions`.
- **PAUSED with DOM ERROR** — ChatGPT's DOM changed (composer/send button not
  found, or the send did not clear the composer). Read the error text in the
  popup, update the selectors in `extension\content.js`, reload the extension
  at `chrome://extensions`, then click **Resume**. The extension never blind-
  retries sends.
- **Port already in use** — `STOP_LOCAL_HANDS.bat`, then start again.
- **Oversized output** — results over `max_inline_bytes` come back as
  `truncated: true` with head/tail and a `full_output_path` under `output\`.
