// Local Hands popup logic. Talks to the service worker only; the page DOM
// is never touched from here.

"use strict";

const $ = function (id) { return document.getElementById(id); };

const MODE_LABELS = {
  safe: "Safe",
  workspace_full_access: "Workspace Full Access",
  full_pc_access: "Full PC Access",
};

function send(msg) {
  return new Promise(function (resolve) {
    function attempt(retriesLeft) {
      chrome.runtime.sendMessage(msg, function (resp) {
        if (chrome.runtime.lastError) {
          const errText = chrome.runtime.lastError.message || "";
          // MV3 wake-up race: the first message can arrive while the sleeping
          // service worker is still starting, before its onMessage listener is
          // registered ("Receiving end does not exist."). Retry once after a
          // short delay so the worker has time to come up.
          if (retriesLeft > 0 &&
              /receiving end does not exist|could not establish connection/i.test(errText)) {
            setTimeout(function () { attempt(retriesLeft - 1); }, 200);
            return;
          }
          resolve({ ok: false, channel: true,
                    error: { code: "RUNTIME", message: errText } });
        } else {
          resolve(resp || { ok: false, channel: true,
                            error: { code: "NO_RESPONSE",
                                     message: "no response (channel closed before sendResponse)" } });
        }
      });
    }
    attempt(1);
  });
}

function fmtTime(ts) {
  const d = new Date(ts);
  return d.toTimeString().slice(0, 8);
}

async function activeTabUrl() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab ? tab.url : null;
}

async function renderCalls() {
  const resp = await send({ type: "LOCAL_HANDS_STATE" });
  const list = $("calls");
  list.textContent = "";
  if (!resp.ok) return;
  const calls = (resp.state && resp.state.lh_last_calls) || [];
  if (!calls.length) {
    const li = document.createElement("li");
    li.textContent = "(none yet)";
    li.style.color = "var(--muted)";
    list.appendChild(li);
  }
  for (const c of calls) {
    const li = document.createElement("li");
    li.className = c.status === "ok" || (c.status || "").indexOf("ok") === 0 ? "ok" : "error";
    li.textContent = fmtTime(c.ts) + " " + (c.tool || "?") + " → " + (c.status || "?") +
      (c.error ? " (" + c.error + ")" : "") + (c.id ? " [" + c.id + "]" : "");
    li.title = li.textContent;
    list.appendChild(li);
  }
}

async function render() {
  // bridge health
  const health = await send({ type: "LOCAL_HANDS_HEALTH" });
  const dot = $("dot");
  const connected = Boolean(health && health.healthy);
  dot.className = "dot " + (connected ? "ok" : "bad");
  let bridgeText = connected ? "Connected" : "Disconnected";
  if (!connected && health) {
    const rawErr = health.error;
    const reason = (rawErr && typeof rawErr === "object") ? rawErr.message
      : (typeof rawErr === "string" ? rawErr
        : (health.status != null ? "HTTP " + health.status : ""));
    if (reason) bridgeText += " — " + reason;
  }
  $("bridge-status").textContent = bridgeText;

  // port + address
  const stateResp = await send({ type: "LOCAL_HANDS_STATE" });
  const port = (stateResp && stateResp.port) || 8787;
  $("addr").textContent = "127.0.0.1:" + port;

  // mode
  const caps = await send({ type: "LOCAL_HANDS_CAPABILITIES" });
  const mode = caps && caps.mode;
  $("mode").textContent = connected
    ? (MODE_LABELS[mode] || (mode ? String(mode) : "unknown"))
    : "— (bridge offline)";

  // current chat automation state
  const url = await activeTabUrl();
  const isChat = Boolean(url && url.indexOf("https://chatgpt.com/") === 0);
  const st = (stateResp && stateResp.state) || {};
  const enabled = isChat && st.lh_enabled_chats && Boolean(st.lh_enabled_chats[url]);
  const paused = Boolean(st.lh_paused);
  const stopped = Boolean(st.lh_stopped);
  let label;
  if (stopped) label = "OFF (emergency STOP)";
  else if (!isChat) label = "— (not a chatgpt.com tab)";
  else if (!enabled) label = "OFF (not initialized)";
  else if (paused) label = "PAUSED";
  else label = "ON";
  $("chat-state").textContent = label;

  // pause / resume visibility
  $("btn-pause").classList.toggle("hidden", paused || stopped);
  $("btn-resume").classList.toggle("hidden", !(paused || stopped));
  $("btn-pause").textContent = "Pause: off";

  // error box
  const errBox = $("error-box");
  const lastError = st.lh_last_error;
  if (lastError) {
    errBox.textContent = lastError;
    errBox.classList.remove("hidden");
  } else {
    errBox.classList.add("hidden");
  }

  renderCalls();
}

document.addEventListener("DOMContentLoaded", function () {
  render();

  $("btn-refresh").addEventListener("click", function () { render(); });

  $("btn-init").addEventListener("click", async function () {
    const url = await activeTabUrl();
    if (!url || url.indexOf("https://chatgpt.com/") !== 0) {
      alert("Open a chatgpt.com chat tab first.");
      return;
    }
    const resp = await send({ type: "LOCAL_HANDS_INIT", chatUrl: url });
    if (resp && resp.ok) {
      render();
    } else {
      alert("Initialize failed: " + JSON.stringify(resp && resp.error));
    }
  });

  $("btn-pause").addEventListener("click", async function () {
    await send({ type: "LOCAL_HANDS_PAUSE", value: true });
    render();
  });

  $("btn-resume").addEventListener("click", async function () {
    await send({ type: "LOCAL_HANDS_RESUME" });
    render();
  });

  $("btn-stop").addEventListener("click", async function () {
    await send({ type: "LOCAL_HANDS_STOP" });
    render();
  });
});
