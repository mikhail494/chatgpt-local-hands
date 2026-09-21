// Local Hands — content script. Injected ONLY into https://chatgpt.com/*.
// Watches completed assistant turns for LOCAL_HANDS protocol blocks,
// asks the service worker to execute them, then types the RESULT/RESULTS
// reply into the composer. Never executes from user messages, never
// executes while the model is still generating, and pauses (no retries)
// on DOM failures.

(function () {
  "use strict";
  if (window.__LOCAL_HANDS_CONTENT_LOADED__) return;
  window.__LOCAL_HANDS_CONTENT_LOADED__ = true;

  const ASSISTANT_SELECTORS = [
    '[data-message-author-role="assistant"]',
    '[data-turn-author="assistant"]',
    '[data-message-author-role="bot"]',
  ];
  const STOP_BUTTON_SELECTORS = [
    '[data-testid="stop-button"]',
    'button[aria-label*="Stop generating" i]',
    'button[aria-label="Stop" i]',
    '[data-stop-button="true"]',
  ];
  const COMPOSER_SELECTORS = [
    'textarea',
    '[contenteditable="true"]',
  ];
  const SEND_BUTTON_SELECTORS = [
    'button[data-testid="send-button"]',
    'button[aria-label*="Send" i]',
    'button[aria-label*="send" i]',
  ];

  const STABILITY_MS = 1000;
  const TICK_MS = 500;

  const state = {
    processedEls: new WeakSet(),
    stability: new Map(), // el -> { text, since }
    sending: false,
    paused: false,
    stopped: false,
  };

  function chatUrl() {
    return location.href;
  }

  function domError(message) {
    state.paused = true;
    chrome.runtime.sendMessage({
      type: "LOCAL_HANDS_PAUSE", value: true,
    }).catch(function () {});
    chrome.storage.local.set({
      lh_last_error: "DOM ERROR: " + message,
    }).catch(function () {});
    // no blind retries: stays paused until the user resumes from the popup
  }

  function sendToBackground(msg) {
    return new Promise(function (resolve) {
      try {
        chrome.runtime.sendMessage(msg, function (resp) {
          if (chrome.runtime.lastError) {
            resolve({ ok: false, error: { code: "RUNTIME", message: chrome.runtime.lastError.message } });
          } else {
            resolve(resp || { ok: false, error: { code: "NO_RESPONSE", message: "no response" } });
          }
        });
      } catch (e) {
        resolve({ ok: false, error: { code: "RUNTIME", message: String(e) } });
      }
    });
  }

  function findAssistantMessages() {
    for (const sel of ASSISTANT_SELECTORS) {
      const els = document.querySelectorAll(sel);
      if (els.length) return Array.prototype.slice.call(els);
    }
    return [];
  }

  function isGenerating() {
    for (const sel of STOP_BUTTON_SELECTORS) {
      const el = document.querySelector(sel);
      if (el && el.offsetParent !== null) return true;
    }
    return false;
  }

  function parseBlocks(text) {
    const found = [];
    const callRe = /\[\[LOCAL_HANDS_V1:CALL\]\]([\s\S]*?)\[\[\/LOCAL_HANDS_V1:CALL\]\]/g;
    const batchRe = /\[\[LOCAL_HANDS_V1:BATCH\]\]([\s\S]*?)\[\[\/LOCAL_HANDS_V1:BATCH\]\]/g;
    let m;
    while ((m = callRe.exec(text)) !== null) {
      found.push({ at: m.index, kind: "CALL", raw: m[1] });
    }
    while ((m = batchRe.exec(text)) !== null) {
      found.push({ at: m.index, kind: "BATCH", raw: m[1] });
    }
    found.sort(function (a, b) { return a.at - b.at; });
    return found.map(function (b) { return { kind: b.kind, raw: b.raw }; });
  }

  function isFinished(el) {
    if (isGenerating()) return false;
    const text = el.innerText || el.textContent || "";
    const now = Date.now();
    const prev = state.stability.get(el);
    if (prev && prev.text === text) {
      return (now - prev.since) >= STABILITY_MS;
    }
    state.stability.set(el, { text: text, since: now });
    return false;
  }

  function turnId(el, index) {
    const id = el.getAttribute && el.getAttribute("data-message-id");
    if (id) return "msg-" + id;
    return "turn-" + index;
  }

  function buildReplyText(results) {
    const hasBatch = results.some(function (r) { return r && r.kind === "BATCH"; });
    if (hasBatch) {
      const payload = { results: results.map(function (r) {
        if (r && r.kind === "BATCH") {
          return { kind: "BATCH", status: r.status,
                   results: r.results, error: r.error };
        }
        return r;
      }) };
      return "[[LOCAL_HANDS_V1:RESULTS]]\n" + JSON.stringify(payload) +
        "\n[[/LOCAL_HANDS_V1:RESULTS]]";
    }
    const payload = { results: results };
    return "[[LOCAL_HANDS_V1:RESULT]]\n" + JSON.stringify(payload.results) +
      "\n[[/LOCAL_HANDS_V1:RESULT]]";
  }

  function visibleComposer() {
    for (const sel of COMPOSER_SELECTORS) {
      const els = document.querySelectorAll(sel);
      for (let i = els.length - 1; i >= 0; i--) {
        const el = els[i];
        const visible = el.offsetParent !== null || el.getClientRects().length > 0;
        if (visible) return el;
      }
    }
    return null;
  }

  function setComposerText(el, text) {
    if (el.tagName === "TEXTAREA") {
      const proto = window.HTMLTextAreaElement.prototype;
      const desc = Object.getOwnPropertyDescriptor(proto, "value");
      if (desc && desc.set) {
        desc.set.call(el, text);
      } else {
        el.value = text;
      }
    } else {
      el.textContent = text;
    }
    el.focus();
    el.dispatchEvent(new Event("input", { bubbles: true }));
    el.dispatchEvent(new Event("change", { bubbles: true }));
  }

  function composerEmpty(el) {
    if (!el) return false;
    if (el.tagName === "TEXTAREA") return (el.value || "").trim() === "";
    return (el.textContent || "").trim() === "";
  }

  function findSendButton() {
    for (const sel of SEND_BUTTON_SELECTORS) {
      const els = document.querySelectorAll(sel);
      for (const el of els) {
        if (el.disabled) continue;
        const visible = el.offsetParent !== null || el.getClientRects().length > 0;
        if (visible) return el;
      }
    }
    return null;
  }

  function sleep(ms) {
    return new Promise(function (r) { setTimeout(r, ms); });
  }

  async function sendToChat(text) {
    if (state.sending) return false;
    state.sending = true;
    try {
      const composer = visibleComposer();
      if (!composer) {
        domError("COMPOSER_NOT_FOUND");
        return false;
      }
      setComposerText(composer, text);
      await sleep(300);
      const btn = findSendButton();
      if (btn) {
        btn.click();
      } else {
        composer.dispatchEvent(new KeyboardEvent("keydown", {
          key: "Enter", code: "Enter", keyCode: 13, which: 13,
          bubbles: true, cancelable: true,
        }));
      }
      await sleep(900);
      if (!composerEmpty(composer)) {
        domError("SEND_FAILED: composer still holds the message after send attempt");
        return false;
      }
      return true;
    } finally {
      state.sending = false;
    }
  }

  async function handleTurn(el, index) {
    const text = el.innerText || el.textContent || "";
    const blocks = parseBlocks(text);
    if (!blocks.length) {
      state.processedEls.add(el);
      return;
    }
    state.processedEls.add(el);
    const id = turnId(el, index);
    const resp = await sendToBackground({
      type: "LOCAL_HANDS_EXECUTE",
      chatUrl: chatUrl(),
      messageId: id,
      blocks: blocks,
    });
    if (!resp || resp.executed !== true) {
      const reason = (resp && resp.reason) ||
        (resp && resp.error && resp.error.code) || "unknown";
      const reply = "[[LOCAL_HANDS_V1:RESULT]]\n" +
        JSON.stringify([{ id: id, tool: null, status: "skipped",
                          error: { code: reason ? "NOT_EXECUTED_" + String(reason).toUpperCase() : "NOT_EXECUTED",
                                   message: "execution was not run (" + String(reason) + ")" } }]) +
        "\n[[/LOCAL_HANDS_V1:RESULT]]";
      await sendToChat(reply);
      return;
    }
    const reply = buildReplyText(resp.results || []);
    await sendToChat(reply);
  }

  async function tick() {
    if (state.stopped || state.paused) return;
    const msgs = findAssistantMessages();
    if (!msgs.length) return;
    const latest = msgs[msgs.length - 1];
    if (state.processedEls.has(latest)) return;
    if (!isFinished(latest)) return;
    await handleTurn(latest, msgs.length - 1);
  }

  async function refreshFlags() {
    try {
      const s = await chrome.storage.local.get(["lh_paused", "lh_stopped"]);
      state.paused = Boolean(s.lh_paused);
      state.stopped = Boolean(s.lh_stopped);
    } catch (e) { /* storage unavailable; keep current flags */ }
  }

  chrome.storage.onChanged.addListener(function (changes, area) {
    if (area === "local") refreshFlags();
  });

  chrome.runtime.onMessage.addListener(function (msg) {
    if (!msg) return;
    if (msg.type === "LOCAL_HANDS_INIT") {
      // handshake: announce readiness to the chat by sending the fixed body
      sendToChat(msg.body || "[LOCAL_HANDS_V1_READY]").then(function (ok) {
        if (!ok) {
          chrome.runtime.sendMessage({ type: "LOCAL_HANDS_CLEAR_ERROR" }).catch(function () {});
        }
      });
      return;
    }
    if (msg.type === "LOCAL_HANDS_RESUME_FLAGS") {
      state.paused = false;
      state.stopped = false;
      return;
    }
  });

  refreshFlags();
  setInterval(function () {
    tick().catch(function () { /* next tick retries only for non-DOM causes */ });
  }, TICK_MS);
})();
