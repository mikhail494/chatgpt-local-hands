// Local Hands — MV3 service worker.
// Owns: bridge HTTP calls, browser.* tool execution,
// dedupe of request ids, pause/stop state, last-calls log.
// All localhost HTTP is done here (never from page context).

const BRIDGE_HOST = "127.0.0.1";
const DEFAULT_PORT = 8787;
const FETCH_TIMEOUT_MS = 60000;
const READ_PAGE_MAX_CHARS = 20000;
const MAX_LAST_CALLS = 20;
const MAX_PROCESSED_IDS = 500;

const STORAGE_KEYS = {
  port: "lh_port",
  enabled: "lh_enabled_chats",
  paused: "lh_paused",
  stopped: "lh_stopped",
  lastCalls: "lh_last_calls",
  processed: "lh_processed_ids",
  lastError: "lh_last_error",
};

async function storeGet(keys) {
  return chrome.storage.local.get(keys);
}

async function storeSet(obj) {
  return chrome.storage.local.set(obj);
}

async function bridgeUrl(path, port) {
  const p = port || DEFAULT_PORT;
  return "http://" + BRIDGE_HOST + ":" + p + path;
}

async function bridgeFetch(path, port, options) {
  const opts = options || {};
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(await bridgeUrl(path, port), Object.assign({}, opts, {
      headers: headers,
      signal: controller.signal,
    }));
  } catch (e) {
    throw { code: "BRIDGE_UNREACHABLE", message: "bridge not reachable: " + e.message };
  } finally {
    clearTimeout(timer);
  }
  let body = null;
  try {
    body = await resp.json();
  } catch (e) {
    body = null;
  }
  return { status: resp.status, body: body };
}

async function recordCall(entry) {
  const s = await storeGet([STORAGE_KEYS.lastCalls]);
  const list = Array.isArray(s[STORAGE_KEYS.lastCalls]) ? s[STORAGE_KEYS.lastCalls] : [];
  list.unshift({
    ts: Date.now(),
    id: entry.id || null,
    tool: entry.tool || null,
    status: entry.status || null,
    error: entry.error || null,
  });
  while (list.length > MAX_LAST_CALLS) list.pop();
  await storeSet({ [STORAGE_KEYS.lastCalls]: list });
}

async function isProcessed(id) {
  if (!id) return false;
  const s = await storeGet([STORAGE_KEYS.processed]);
  const map = s[STORAGE_KEYS.processed] || {};
  if (map[id]) return true;
  map[id] = Date.now();
  const ids = Object.keys(map);
  if (ids.length > MAX_PROCESSED_IDS) {
    ids.sort((a, b) => map[a] - map[b]);
    for (const k of ids.slice(0, ids.length - MAX_PROCESSED_IDS)) delete map[k];
  }
  await storeSet({ [STORAGE_KEYS.processed]: map });
  return false;
}

// ---------- browser.* tools (extension-side only) ----------

async function browserStatus(state) {
  return {
    ok: true,
    bridge: { host: BRIDGE_HOST, port: state.port || DEFAULT_PORT },
    enabled_chats: state.enabled || {},
    paused: Boolean(state.paused),
    stopped: Boolean(state.stopped),
  };
}

async function browserTabs() {
  const tabs = await chrome.tabs.query({});
  return tabs.map((t) => ({
    id: t.id,
    url: t.url || null,
    title: t.title || null,
    active: Boolean(t.active),
  }));
}

async function browserOpen(args) {
  const url = args && args.url;
  if (!url) throw { code: "BAD_ARGS", message: "browser.open requires args.url" };
  const tab = await chrome.tabs.create({ url: url });
  return { id: tab.id, url: url };
}

async function browserNavigate(args) {
  const tabId = args && args.tabId;
  const url = args && args.url;
  if (!tabId || !url) throw { code: "BAD_ARGS", message: "browser.navigate requires args.tabId and args.url" };
  const tab = await chrome.tabs.update(tabId, { url: url });
  return { id: tab.id, url: url };
}

async function browserReadPage(args) {
  const tabId = args && (args.tabId != null ? args.tabId : null);
  let targetTab;
  if (tabId != null) {
    targetTab = await chrome.tabs.get(tabId);
  } else {
    const [active] = await chrome.tabs.query({ active: true, currentWindow: true });
    if (!active) throw { code: "NO_ACTIVE_TAB", message: "no active tab" };
    targetTab = active;
  }
  const pageUrl = targetTab.url || "";
  if (/^chrome:|^edge:|^about:|^chrome-extension:|^chrome-web-store:/i.test(pageUrl) ||
      pageUrl.startsWith("https://chromewebstore.google.com") ||
      pageUrl.startsWith("view-source:")) {
    throw { code: "UNSUPPORTED_PAGE",
            message: "cannot read this protected page: " + pageUrl };
  }
  const results = await chrome.scripting.executeScript({
    target: { tabId: targetTab.id },
    func: function () {
      return {
        url: location.href,
        title: document.title,
        text: document.body ? document.body.innerText : "",
      };
    },
  });
  const data = (results && results[0] && results[0].result) || {};
  let text = data.text || "";
  const truncated = text.length > READ_PAGE_MAX_CHARS;
  if (truncated) text = text.slice(0, READ_PAGE_MAX_CHARS);
  return {
    tabId: targetTab.id,
    url: data.url || pageUrl,
    title: data.title || targetTab.title || null,
    bodyText: text,
    truncated: truncated,
    total_chars: (data.text || "").length,
  };
}

async function execBrowserTool(tool, args) {
  switch (tool) {
    case "browser.status":
      return browserStatus(await storeGet(Object.keys(STORAGE_KEYS)));
    case "browser.tabs":
      return browserTabs();
    case "browser.open":
      return browserOpen(args);
    case "browser.navigate":
      return browserNavigate(args);
    case "browser.read_page":
      return browserReadPage(args);
    default:
      throw { code: "UNKNOWN_TOOL", message: "unknown browser tool: " + tool };
  }
}

// ---------- execution of one CALL ----------

async function execCall(call, state) {
  const id = call && call.id;
  const tool = call && String(call.tool || "");
  const args = call && call.args;

  if (await isProcessed(id)) {
    return { id: id, tool: tool, status: "cached",
             message: "duplicate id; not re-executed" };
  }
  if (!tool) {
    return { id: id, tool: "", status: "error",
             error: { code: "BAD_CALL", message: "call requires a tool" } };
  }
  try {
    let value;
    if (tool.startsWith("browser.")) {
      value = await execBrowserTool(tool, args || {});
    } else {
      const r = await bridgeFetch("/invoke", state.port, {
        method: "POST",
        body: JSON.stringify({ id: id, tool: tool, args: args || {} }),
      });
      if (r.status !== 200) {
        return { id: id, tool: tool, status: "error",
                 error: (r.body && r.body.error) || { code: "BRIDGE_ERROR", message: "bridge HTTP " + r.status },
                 http: r.status };
      }
      // r.body is the bridge result envelope {id, tool, status, result|error, elapsed_ms}
      value = r.body.result;
      if (r.body.status === "error") {
        await recordCall({ id: id, tool: tool, status: "error",
                           error: r.body.error && r.body.error.code });
        return { id: id, tool: tool, status: "error", error: r.body.error };
      }
      if (r.body.cached) {
        await recordCall({ id: id, tool: tool, status: "ok (cached bridge id)" });
        return { id: id, tool: tool, status: "ok", cached: true, result: value };
      }
    }
    await recordCall({ id: id, tool: tool, status: "ok" });
    return { id: id, tool: tool, status: "ok", result: value };
  } catch (e) {
    const err = (e && e.code) ? { code: e.code, message: e.message }
      : { code: "EXECUTION_ERROR", message: String((e && e.message) || e) };
    await recordCall({ id: id, tool: tool, status: "error", error: err.code });
    return { id: id, tool: tool, status: "error", error: err };
  }
}

async function handleExecute(msg) {
  const state = await storeGet(Object.keys(STORAGE_KEYS));
  const blocked = state[STORAGE_KEYS.stopped] ? "stopped"
    : state[STORAGE_KEYS.paused] ? "paused" : null;
  if (blocked) {
    return { executed: false, reason: blocked, results: [] };
  }
  const blocks = Array.isArray(msg.blocks) ? msg.blocks : [];
  const results = [];
  for (const block of blocks) {
    if (!block) continue;
    if (block.kind === "BATCH") {
      let payload;
      try {
        payload = JSON.parse(block.raw);
      } catch (e) {
        results.push({ kind: "BATCH", status: "error",
                       error: { code: "BAD_JSON", message: "batch block is not valid JSON" } });
        continue;
      }
      const calls = Array.isArray(payload && payload.calls) ? payload.calls : null;
      if (!calls) {
        results.push({ kind: "BATCH", status: "error",
                       error: { code: "BAD_BATCH", message: "batch payload requires a calls array" } });
        continue;
      }
      const batchResults = [];
      for (const call of calls) {
        batchResults.push(await execCall(call, state));
      }
      results.push({ kind: "BATCH", status: "ok", results: batchResults });
    } else {
      let payload;
      try {
        payload = JSON.parse(block.raw);
      } catch (e) {
        results.push({ kind: "CALL", status: "error",
                       error: { code: "BAD_JSON", message: "call block is not valid JSON" } });
        continue;
      }
      const r = await execCall(payload, state);
      results.push({ kind: "CALL", status: r.status === "ok" ? "ok" : "error",
                     id: r.id, tool: r.tool,
                     result: r.result !== undefined ? r.result : undefined,
                     error: r.error || undefined,
                     cached: r.cached ? true : undefined });
    }
  }
  await storeSet({ [STORAGE_KEYS.lastError]: null });
  return { executed: true, results: results };
}

// ---------- init / handshake ----------

const HANDSHAKE_BODY =
  "[LOCAL_HANDS_V1_READY]\n" +
  "Local Hands is connected to this chat.\n" +
  "Bridge tools (executed on this PC by the local bridge): fs.read, fs.list, fs.stat, fs.write, fs.patch, fs.mkdir, fs.move, fs.copy, fs.delete, shell.powershell, shell.cmd, process.list, process.start, process.kill, file.tail.\n" +
  "Browser tools (executed by this extension only): browser.status, browser.tabs, browser.open, browser.navigate, browser.read_page.\n" +
  "Protocol: to call one tool, reply with exactly one block:\n" +
  "[[LOCAL_HANDS_V1:CALL]]\n" +
  "{\"id\":\"<unique-id>\",\"tool\":\"<tool>\",\"args\":{...}}\n" +
  "[[/LOCAL_HANDS_V1:CALL]]\n" +
  "To batch multiple tools in one reply:\n" +
  "[[LOCAL_HANDS_V1:BATCH]]\n" +
  "{\"calls\":[{\"id\":\"...\",\"tool\":\"...\",\"args\":{...}}, ...]}\n" +
  "[[/LOCAL_HANDS_V1:BATCH]]\n" +
  "Your next user message will contain the execution outcome as a [[LOCAL_HANDS_V1:RESULT]] block (single call) or a [[LOCAL_HANDS_V1:RESULTS]] block (batch).";

async function ensureContentScript(tabId) {
  try {
    await chrome.scripting.executeScript({
      target: { tabId: tabId },
      files: ["content.js"],
    });
  } catch (e) {
    // tab may not be a content-scriptable page; init will report it
    throw { code: "INJECT_FAILED", message: "could not inject content script: " + e.message };
  }
}

async function handleInit(msg) {
  const chatUrl = msg && msg.chatUrl;
  if (!chatUrl) throw { code: "BAD_ARGS", message: "init requires chatUrl" };
  const state = await storeGet([STORAGE_KEYS.enabled]);
  const enabled = state[STORAGE_KEYS.enabled] || {};
  enabled[chatUrl] = { ts: Date.now() };
  await storeSet({
    [STORAGE_KEYS.enabled]: enabled,
    [STORAGE_KEYS.stopped]: false,
    [STORAGE_KEYS.lastError]: null,
  });
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (tab && tab.id != null) {
    await ensureContentScript(tab.id);
    await chrome.tabs.sendMessage(tab.id, { type: "LOCAL_HANDS_INIT", body: HANDSHAKE_BODY });
  }
  return { ok: true, chatUrl: chatUrl };
}

// ---------- popup / misc ----------

async function handleHealth(msg) {
  const state = await storeGet([STORAGE_KEYS.port]);
  const url = await bridgeUrl("/health", state[STORAGE_KEYS.port]);
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
  let resp;
  try {
    resp = await fetch(url, { method: "GET", signal: controller.signal });
  } catch (e) {
    return { healthy: false, status: null, error: (e && e.message) || String(e) };
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  if (resp.ok) {
    try {
      data = await resp.json();
    } catch (e) {
      data = null;
    }
  }
  // Bridge contract: HTTP success AND JSON body with status === "ok" (or ok === true).
  const healthy = resp.ok && data && (data.status === "ok" || data.ok === true);
  return {
    healthy: Boolean(healthy),
    status: resp.status,
    error: healthy ? null : (resp.ok ? "unexpected health body" : "bridge HTTP " + resp.status),
  };
}

async function handleCapabilities() {
  const state = await storeGet([STORAGE_KEYS.port]);
  try {
    const r = await bridgeFetch("/capabilities", state[STORAGE_KEYS.port], { method: "GET" });
    if (r.status !== 200) return { mode: null, status: r.status };
    return { mode: r.body && r.body.mode, status: 200 };
  } catch (e) {
    return { mode: null, error: e.message };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  (async () => {
    try {
      switch (msg && msg.type) {
        case "LOCAL_HANDS_EXECUTE":
          sendResponse(await handleExecute(msg));
          break;
        case "LOCAL_HANDS_INIT":
          sendResponse(await handleInit(msg));
          break;
        case "LOCAL_HANDS_HEALTH":
          sendResponse(await handleHealth(msg));
          break;
        case "LOCAL_HANDS_CAPABILITIES":
          sendResponse(await handleCapabilities());
          break;
        case "LOCAL_HANDS_PAUSE":
          await storeSet({ [STORAGE_KEYS.paused]: Boolean(msg && msg.value) });
          sendResponse({ ok: true });
          break;
        case "LOCAL_HANDS_STOP": {
          await storeSet({
            [STORAGE_KEYS.stopped]: true,
            [STORAGE_KEYS.paused]: true,
          });
          sendResponse({ ok: true });
          break;
        }
        case "LOCAL_HANDS_RESUME":
          await storeSet({
            [STORAGE_KEYS.stopped]: false,
            [STORAGE_KEYS.paused]: false,
          });
          sendResponse({ ok: true });
          break;
        case "LOCAL_HANDS_STATE": {
          const s = await storeGet(Object.keys(STORAGE_KEYS));
          sendResponse({ ok: true, state: s,
                         port: s[STORAGE_KEYS.port] || DEFAULT_PORT });
          break;
        }
        case "LOCAL_HANDS_CLEAR_ERROR":
          await storeSet({ [STORAGE_KEYS.lastError]: null });
          sendResponse({ ok: true });
          break;
        default:
          sendResponse({ ok: false, error: "unknown message type" });
      }
    } catch (e) {
      const err = (e && e.code) ? { code: e.code, message: e.message }
        : { code: "INTERNAL", message: String((e && e.message) || e) };
      sendResponse({ ok: false, error: err });
    }
  })();
  return true; // keep the response channel open for the async reply
});
