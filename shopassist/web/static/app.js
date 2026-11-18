const transcript = document.querySelector("#transcript");
const evidence = document.querySelector("#evidence");
const events = document.querySelector("#events");
const scenarios = document.querySelector("#scenarios");
const banner = document.querySelector("#banner");
const message = document.querySelector("#message");
const send = document.querySelector("#send");
const mode = document.querySelector("#mode");
let sessionId = null;
let busy = false;

function setBusy(value) {
  busy = value;
  send.disabled = value;
  send.textContent = value ? "Working..." : "Send";
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

function renderSide(turn) {
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
  return sessionId;
}

async function sendText(text) {
  clearError();
  setBusy(true);
  addBubble("user", text);
  try {
    await ensureSession();
    const response = await fetch("/api/sessions/" + sessionId + "/message", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text}),
    });
    const payload = await response.json();
    if (!response.ok) {
      showError(payload.error_code + (payload.details && payload.details.hint ? ": " + payload.details.hint : ""));
      return;
    }
    addBubble("assistant", payload.reply_text || "");
    renderSide(payload);
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
    if (mode.value !== "mock") {
      await ensureSession();
    }
    const response = await fetch("/api/sessions/demo/run", {
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
    mark.textContent = payload.passed ? "Case checks passed." : "Case checks found a gap.";
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

loadScenarios();
const params = new URLSearchParams(window.location.search);
if (params.get("autorun")) {
  runCase(params.get("autorun"));
}
