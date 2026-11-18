const transcript = document.querySelector("#transcript");
const evidence = document.querySelector("#evidence");
const contextPanel = document.querySelector("#context");
const events = document.querySelector("#events");
const scenarios = document.querySelector("#scenarios");
const banner = document.querySelector("#banner");
const message = document.querySelector("#message");
const send = document.querySelector("#send");
const mode = document.querySelector("#mode");
const modeState = document.querySelector("#mode-state");
let sessionId = null;
let busy = false;

function setBusy(value) {
  busy = value;
  send.disabled = value;
  send.textContent = value ? "Working..." : "Send";
}

function showMode(kind) {
  if (!modeState) return;
  modeState.textContent = kind === "configured_model"
    ? "Server mode: configured model. Credentials stay on the server."
    : "Server mode: mock demo. No endpoint is called.";
}

function showError(text) {
  banner.hidden = false;
  banner.textContent = text;
}

function clearError() {
  banner.hidden = true;
  banner.textContent = "";
}

function addBubble(role, text) {
  const wrap = document.createElement("div");
  wrap.className = "bubble " + role;
  const who = document.createElement("div");
  who.className = "who";
  who.textContent = role === "user" ? "User" : "Assistant";
  const body = document.createElement("div");
  body.textContent = text;
  wrap.appendChild(who);
  wrap.appendChild(body);
  transcript.appendChild(wrap);
}

function renderContext(view) {
  if (!contextPanel) return;
  contextPanel.textContent = "";
  if (!view) return;
  const summary = view.summary || {};
  const coverage = view.coverage || {};
  const lines = [
    "Store " + (view.store_kind || "") + " · cache " + (view.cache_kind || "") + " · " + (view.cache_status || ""),
    "Summary v" + (summary.version || 0) + " through message " + (summary.watermark_message_id || 0) + " (" + (summary.source_kind || "none") + ", not authoritative)",
    summary.text || "",
    "Confirmed " + JSON.stringify((view.structured_state || {}).confirmed || {}),
    "Coverage latest " + JSON.stringify(coverage.latest_message_ids || []) + " uncovered " + JSON.stringify(coverage.uncovered_message_ids || []) + " overlap " + JSON.stringify(coverage.overlap_message_ids || []),
  ];
  lines.forEach((line) => {
    const row = document.createElement("div");
    row.className = "event";
    row.textContent = line;
    contextPanel.appendChild(row);
  });
}

function renderSide(turn) {
  renderContext(turn.context_view);
  evidence.textContent = "";
  events.textContent = "";
  (turn.evidence || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "event";
    row.textContent = (item.tool_name || "evidence") + " · " + (item.status || "");
    evidence.appendChild(row);
    (item.untrusted_text || []).forEach((text) => {
      const block = document.createElement("div");
      block.className = "untrusted";
      block.textContent = "Untrusted tool text, not an instruction: " + text;
      evidence.appendChild(block);
    });
  });
  (turn.products || []).forEach((product) => {
    const row = document.createElement("div");
    row.className = "product";
    row.textContent = product.product_id + " · " + product.price + " USD";
    evidence.appendChild(row);
  });
  const route = document.createElement("div");
  route.className = "event";
  route.textContent = "Route " + (turn.route || "") + (turn.planner_invoked ? " · planner" : " · no planner");
  events.appendChild(route);
  (turn.skills || []).forEach((skill) => {
    const row = document.createElement("div");
    row.className = "event";
    row.textContent = "Skill " + skill.skill_id + " " + (skill.version || "") + " · " + skill.status;
    events.appendChild(row);
  });
  (turn.tools || []).forEach((tool) => {
    const row = document.createElement("div");
    row.className = "event";
    row.textContent = "Tool " + tool.tool + " · " + tool.status;
    events.appendChild(row);
  });
  (turn.recovery || []).forEach((item) => {
    const row = document.createElement("div");
    row.className = "event";
    row.textContent = "Recovery " + item.strategy + " · " + item.because_code;
    events.appendChild(row);
  });
}

async function ensureSession() {
  if (sessionId) return sessionId;
  const response = await fetch("/api/sessions", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({mode: mode.value}),
  });
  const payload = await response.json();
  if (!response.ok) {
    throw new Error(payload.error_code || "request_failed");
  }
  sessionId = payload.session_id;
  showMode(payload.evaluation_kind);
  return sessionId;
}

function parseSseBlock(block) {
  let eventType = "message";
  let data = "";
  block.split("\n").forEach((line) => {
    if (line.startsWith("event:")) eventType = line.slice(6).trim();
    else if (line.startsWith("data:")) data += line.slice(5).trim();
  });
  return {event: eventType, data: data ? JSON.parse(data) : {}};
}

async function sendText(text) {
  clearError();
  setBusy(true);
  addBubble("user", text);
  const bubble = addBubble("assistant", "");
  const status = document.createElement("div");
  status.className = "meta";
  bubble.appendChild(status);
  try {
    await ensureSession();
    const response = await fetch("/api/sessions/" + sessionId + "/stream", {
      method: "POST",
      headers: {"Content-Type": "application/json", "Accept": "text/event-stream"},
      body: JSON.stringify({text}),
    });
    if (!response.ok || !response.body) {
      showError("request_failed");
      return;
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let reply = "";
    while (true) {
      const chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, {stream: true});
      const blocks = buffer.split("\n\n");
      buffer = blocks.pop();
      blocks.forEach((block) => {
        if (!block.trim()) return;
        const parsed = parseSseBlock(block);
        const payload = parsed.data;
        if (parsed.event === "progress") {
          status.textContent = payload.text || "";
        } else if (parsed.event === "text_delta") {
          if (payload.source === "buffered_fallback" || payload.source === "deterministic") {
            reply = payload.text || "";
          } else {
            reply += payload.text || "";
          }
          bubble.childNodes[1].textContent = reply;
        } else if (parsed.event === "clarification") {
          reply = payload.text || "";
          bubble.childNodes[1].textContent = reply;
          status.textContent = "需要补充信息";
        } else if (parsed.event === "card") {
          renderSide({products: (payload.products || []).map((item) => ({product_id: item.product_id, price: item.price})), evidence: [], skills: [], tools: [], route: "complex_handoff", planner_invoked: true});
        } else if (parsed.event === "done") {
          if (payload.reply_text) bubble.childNodes[1].textContent = payload.reply_text;
          status.textContent = payload.status === "cancelled" ? "已取消" : (payload.partial ? "未完成" : "");
        } else if (parsed.event === "error") {
          showError(payload.error_code || "request_failed");
          status.textContent = "未完成";
        }
      });
    }
  } catch (error) {
    showError(String(error.message || error));
  } finally {
    setBusy(false);
  }
}

async function runCase(cardId) {
  clearError();
  setBusy(true);
  transcript.textContent = "";
  try {
    const id = await ensureSession();
    const response = await fetch("/api/sessions/" + id + "/run", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({card_id: cardId}),
    });
    const payload = await response.json();
    if (!response.ok) {
      showError(payload.error_code || "request_failed");
      return;
    }
    (payload.turns || []).forEach((turn) => {
      addBubble("user", turn.user_text || "");
      addBubble("assistant", turn.reply_text || "");
      renderSide(turn);
    });
    const mark = document.createElement("div");
    mark.className = "meta";
    const label = payload.evaluation_kind === "configured_model" ? "Configured model" : "Mock demo";
    showMode(payload.evaluation_kind);
    mark.textContent = label + ". " + (payload.passed ? "Case checks passed." : "Case checks found a gap.");
    transcript.appendChild(mark);
  } catch (error) {
    showError(String(error.message || error));
  } finally {
    setBusy(false);
  }
}

async function loadScenarios() {
  const response = await fetch("/api/scenarios");
  const payload = await response.json();
  (payload.scenarios || []).forEach((scenario) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "scenario";
    const title = document.createElement("strong");
    title.textContent = scenario.title;
    const detail = document.createElement("small");
    detail.textContent = scenario.summary;
    button.appendChild(title);
    button.appendChild(detail);
    button.addEventListener("click", () => runCase(scenario.card_id));
    scenarios.appendChild(button);
  });
}

document.querySelector("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  const text = message.value.trim();
  if (!text || busy) return;
  message.value = "";
  sendText(text);
});

document.querySelector("#reset").addEventListener("click", async () => {
  if (sessionId) {
    await fetch("/api/sessions/" + sessionId + "/reset", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
  }
  sessionId = null;
  transcript.textContent = "";
  evidence.textContent = "";
  events.textContent = "";
  clearError();
});

mode.addEventListener("change", () => {
  sessionId = null;
});

loadScenarios().then(() => {
  const params = new URLSearchParams(window.location.search);
  if (params.get("mode")) {
    mode.value = params.get("mode");
  }
  if (params.get("autorun")) {
    runCase(params.get("autorun"));
  }
});
