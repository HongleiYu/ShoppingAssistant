const base = new URL("./", document.baseURI);
const params = new URLSearchParams(location.search);
const transcript = document.querySelector("#transcript");
const chips = document.querySelector("#chips");
const pin = document.querySelector("#pin");
const message = document.querySelector("#message");
const send = document.querySelector("#send");
const cancel = document.querySelector("#cancel");
const status = document.querySelector("#status");
const trace = document.querySelector("#trace");
const traceBody = document.querySelector("#trace-body");
const guideBar = document.querySelector("#guide-bar");
const handoff = document.querySelector("#handoff");
const scenarios = document.querySelector("#scenarios");

let config = null;
let locale = "en";
let worker = null;
let request = 0;
let active = 0;
let ready = false;
let consult = null;
let entryId = "";
let turns = [];
let mode = "own";
let guideGen = 0;
let paceTimer = 0;
let stepIndex = 0;
let skipped = false;
const inflight = new Map();

const t = () => config.ui[locale];
const scenarioById = (id) => config.scenarios.find((item) => item.id === id);
const guidedText = (id) => scenarioById(id).samples[locale];

init();

async function init() {
  config = await (await fetch(new URL("demo-config.json", base))).json();
  locale = chosenLocale();
  entryId = chosenProduct();
  applyChrome();
  document.querySelector("#lang-en").addEventListener("click", () => setLocale("en"));
  document.querySelector("#lang-zh").addEventListener("click", () => setLocale("zh"));
  document.querySelector("#open-trace").addEventListener("click", () => openTrace(turns.at(-1) || null));
  document.querySelector("#close-trace").addEventListener("click", closeTrace);
  document.querySelector("#open-scenarios").addEventListener("click", () => scenarios.showModal());
  document.querySelector("#close-scenarios").addEventListener("click", () => scenarios.close());
  document.querySelector("#skip").addEventListener("click", skipGuide);
  document.querySelector("#replay").addEventListener("click", replayGuide);
  document.querySelector("#own-chat").addEventListener("click", startOwnChat);
  document.querySelector("#reset").addEventListener("click", startOwnChat);
  document.querySelector("#composer").addEventListener("submit", (event) => {
    event.preventDefault();
    submitUser();
  });
  message.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      submitUser();
    }
  });
  cancel.addEventListener("click", () => {
    active = 0;
    cancel.hidden = true;
    send.disabled = !ready;
    status.textContent = t().cancelled;
  });
  worker = new Worker(new URL("worker.js", base), { type: "module" });
  worker.onmessage = onWorker;
  worker.postMessage({ type: "boot", indexURL: new URL("vendor/pyodide/", base).href });
}

function chosenLocale() {
  const query = params.get("locale");
  if (query === "en" || query === "zh") return query;
  try {
    const stored = localStorage.getItem(config.locale_storage_key);
    if (stored === "en" || stored === "zh") return stored;
  } catch (error) {
    /* storage can be blocked; English stays the default */
  }
  return config.default_locale === "zh" ? "zh" : "en";
}

function chosenProduct() {
  const query = params.get("product");
  if (query && /^(?:prod-[a-z0-9-]+|syn-p-[0-9]+)$/.test(query)) return query;
  return config.default_product_id;
}

function setLocale(next) {
  locale = next;
  try {
    localStorage.setItem(config.locale_storage_key, next);
  } catch (error) {
    /* ignore */
  }
  applyChrome();
  renderPin();
  renderTranscript();
}

function applyChrome() {
  const text = t();
  document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  document.title = text.title;
  document.querySelector("#brand-name").textContent = locale === "zh" ? config.brand_zh : config.brand;
  document.querySelector("#guest").textContent = text.guest;
  document.querySelector("#banner").textContent = text.banner;
  document.querySelector("#langs").setAttribute("aria-label", text.language);
  document.querySelector("#lang-en").setAttribute("aria-pressed", locale === "en" ? "true" : "false");
  document.querySelector("#lang-zh").setAttribute("aria-pressed", locale === "zh" ? "true" : "false");
  document.querySelector("#open-scenarios").textContent = text.scenarios;
  document.querySelector("#open-trace").textContent = text.trace;
  document.querySelector("#skip").textContent = text.skip;
  document.querySelector("#reset").textContent = text.reset;
  document.querySelector("#replay").textContent = text.replay;
  document.querySelector("#own-chat").textContent = text.own_chat;
  document.querySelector("#guide-label").textContent = text.guided;
  document.querySelector("#pace-note").textContent = text.pace_note;
  document.querySelector("#scenarios-title").textContent = text.scenarios;
  document.querySelector("#close-scenarios").textContent = text.close_scenarios;
  document.querySelector("#trace-title").textContent = text.trace_title;
  document.querySelector("#close-trace").textContent = text.close_trace;
  document.querySelector("#message-label").textContent = text.message_label;
  message.placeholder = text.placeholder;
  message.maxLength = config.max_message_chars;
  document.querySelector("#send").textContent = text.send;
  document.querySelector("#cancel").textContent = text.cancel;
  chips.replaceChildren();
  for (const id of config.guided.step_ids) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = guidedText(id);
    button.addEventListener("click", () => submitUser(guidedText(id)));
    chips.appendChild(button);
  }
  renderScenarios();
  if (ready && !active) status.textContent = text.ready;
}

function renderScenarios() {
  const list = document.querySelector("#scenario-list");
  list.replaceChildren();
  for (const scenario of config.scenarios) {
    const block = document.createElement("article");
    block.className = "scenario";
    const title = document.createElement("h3");
    title.textContent = scenario.purpose[locale];
    const sample = document.createElement("p");
    sample.textContent = scenario.samples[locale];
    const limits = document.createElement("p");
    limits.className = "note";
    limits.textContent = `${t().scenario_limits}: ${scenario.limits[locale]}`;
    const row = document.createElement("div");
    row.className = "trace-head";
    const use = document.createElement("button");
    use.type = "button";
    use.textContent = t().use_example;
    use.addEventListener("click", () => {
      message.value = scenario.samples[locale];
      scenarios.close();
      message.focus();
    });
    const go = document.createElement("button");
    go.type = "button";
    go.textContent = t().send_example;
    go.addEventListener("click", () => {
      scenarios.close();
      submitUser(scenario.samples[locale]);
    });
    row.append(use, go);
    block.append(title, sample, limits, row);
    list.appendChild(block);
  }
}

function onWorker(event) {
  const data = event.data || {};
  if (data.type === "boot") {
    const text = t();
    status.textContent = data.stage === "sqlite" ? text.boot_sqlite : data.stage === "runtime" ? text.boot_runtime : text.boot_pyodide;
    return;
  }
  if (data.type === "ready") {
    ready = true;
    send.disabled = false;
    postConsult(entryId, "boot");
    return;
  }
  if (data.type === "consult" || data.type === "reset") {
    const meta = inflight.get(data.id);
    if (!meta) return;
    inflight.delete(data.id);
    if (meta.guideGen && meta.guideGen !== guideGen) return;
    finishWait();
    const body = data.payload || {};
    const result = data.type === "reset" ? body.consult : body;
    if (!result || !result.ok) {
      consult = null;
      renderPin();
      renderTranscript();
      showNote(t().unknown_product);
      return;
    }
    consult = result;
    if (meta.kind !== "replay") turns = meta.kind === "own" ? [] : turns;
    renderPin();
    renderTranscript();
    status.textContent = t().ready;
    if (meta.kind === "boot") maybeStartGuide();
    if (meta.kind === "replay") startGuide();
    return;
  }
  if (data.type === "result") {
    const meta = inflight.get(data.id);
    if (!meta) return;
    inflight.delete(data.id);
    finishWait();
    if (meta.guideGen !== guideGen) return;
    if (!data.turn || !data.turn.ok) {
      showNote(t()[data.turn && data.turn.error] || t().runtime_error);
      return;
    }
    turns.push({ origin: meta.origin, user: meta.text, turn: data.turn });
    if (data.turn.consulted_product_id && data.turn.cards && data.turn.cards.length === 1) {
      consult = { ok: true, product_id: data.turn.consulted_product_id, card: data.turn.cards[0], tool: (data.turn.tools || [])[0] || consult.tool };
      renderPin();
    }
    renderTranscript();
    status.textContent = t().done;
    if (meta.origin === "guide") scheduleNext(meta.guideGen);
    return;
  }
  if (data.type === "error") {
    finishWait();
    showNote(t().runtime_error);
  }
}

function postConsult(productId, kind) {
  request += 1;
  active = request;
  inflight.set(active, { kind, guideGen });
  worker.postMessage({ type: kind === "boot" ? "consult" : "reset", id: active, product_id: productId });
}

function maybeStartGuide() {
  if (skipped || params.get("guided") === "0") return;
  const forced = params.get("guided") === "1";
  let seen = false;
  try {
    seen = sessionStorage.getItem(config.guided.seen_key) === "1";
  } catch (error) {
    seen = false;
  }
  if (config.guided.autoplay === "first_session" && seen && !forced) return;
  startGuide();
}

function startGuide() {
  clearPace();
  guideGen += 1;
  const gen = guideGen;
  mode = "guided";
  stepIndex = 0;
  guideBar.hidden = false;
  handoff.hidden = true;
  try {
    sessionStorage.setItem(config.guided.seen_key, "1");
  } catch (error) {
    /* ignore */
  }
  runStep(gen);
}

function runStep(gen) {
  if (gen !== guideGen || mode !== "guided") return;
  const id = config.guided.step_ids[stepIndex];
  if (!id) {
    finishGuide();
    return;
  }
  const text = guidedText(id);
  request += 1;
  active = request;
  inflight.set(active, { origin: "guide", text, guideGen: gen });
  send.disabled = true;
  cancel.hidden = false;
  status.textContent = t().working;
  worker.postMessage({ type: "turn", id: active, text });
}

function scheduleNext(gen) {
  stepIndex += 1;
  if (stepIndex >= config.guided.step_ids.length) {
    finishGuide();
    return;
  }
  clearPace();
  paceTimer = window.setTimeout(() => runStep(gen), Number(config.guided.pace_ms) || 900);
}

function finishGuide() {
  mode = "done";
  guideBar.hidden = false;
  handoff.hidden = false;
  status.textContent = t().ready;
}

function skipGuide() {
  skipped = true;
  clearPace();
  guideGen += 1;
  active = 0;
  mode = "own";
  handoff.hidden = turns.length === 0;
  guideBar.hidden = turns.length === 0;
  finishWait();
  try {
    sessionStorage.setItem(config.guided.seen_key, "1");
  } catch (error) {
    /* ignore */
  }
}

function replayGuide() {
  clearPace();
  guideGen += 1;
  const gen = guideGen;
  skipped = false;
  mode = "guided";
  turns = [];
  handoff.hidden = true;
  guideBar.hidden = false;
  request += 1;
  active = request;
  inflight.set(active, { kind: "replay", guideGen: gen });
  worker.postMessage({ type: "reset", id: active, product_id: entryId });
}

function startOwnChat() {
  clearPace();
  guideGen += 1;
  skipped = true;
  mode = "own";
  turns = [];
  handoff.hidden = true;
  guideBar.hidden = true;
  closeTrace();
  request += 1;
  active = request;
  inflight.set(active, { kind: "own", guideGen });
  worker.postMessage({ type: "reset", id: active, product_id: entryId });
}

function submitUser(text) {
  const value = (text ?? message.value).trim();
  if (!ready || !value || active) return;
  clearPace();
  guideGen += 1;
  mode = "own";
  handoff.hidden = true;
  message.value = "";
  request += 1;
  active = request;
  inflight.set(active, { origin: "user", text: value, guideGen });
  send.disabled = true;
  cancel.hidden = false;
  status.textContent = t().working;
  worker.postMessage({ type: "turn", id: active, text: value });
}

function finishWait() {
  active = 0;
  send.disabled = !ready;
  cancel.hidden = true;
}

function clearPace() {
  if (paceTimer) window.clearTimeout(paceTimer);
  paceTimer = 0;
}

function renderPin() {
  if (!consult || !consult.card) {
    pin.hidden = true;
    pin.replaceChildren();
    return;
  }
  pin.hidden = false;
  const card = consult.card;
  pin.textContent = `${t().consulting}: ${card.name} · ${card.fields.price} ${money(card.currency)} · ${t().fields.stock} ${card.fields.stock}`;
}

function renderTranscript() {
  transcript.replaceChildren();
  if (consult && consult.card) {
    const block = document.createElement("article");
    block.className = "bubble assistant";
    block.appendChild(productCard(consult.card));
    const welcome = document.createElement("div");
    welcome.className = "text";
    welcome.dataset.role = "welcome";
    welcome.textContent = t().welcome;
    block.appendChild(welcome);
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = t().no_checkout;
    block.appendChild(note);
    transcript.appendChild(block);
  }
  turns.forEach((item, index) => transcript.appendChild(turnBlock(item, index)));
  transcript.scrollTop = transcript.scrollHeight;
}

function turnBlock(item, index) {
  const wrap = document.createElement("div");
  const user = document.createElement("article");
  user.className = "bubble user";
  if (item.origin === "guide") user.classList.add("guided-user");
  if (item.origin === "guide") {
    const badge = document.createElement("div");
    badge.className = "badge";
    badge.textContent = `${t().guided} · ${t().fictional_user}`;
    user.appendChild(badge);
  }
  const said = document.createElement("div");
  said.className = "text";
  said.textContent = item.user;
  user.appendChild(said);
  wrap.appendChild(user);
  const assistant = document.createElement("article");
  assistant.className = "bubble assistant";
  const reply = document.createElement("div");
  reply.className = "text";
  reply.textContent = displayReply(item.turn);
  assistant.appendChild(reply);
  if (item.turn.cards && item.turn.cards.length) {
    for (const card of item.turn.cards) assistant.appendChild(productCard(card));
  }
  assistant.appendChild(stepTrace(item.turn, index === turns.length - 1 && mode === "guided"));
  wrap.appendChild(assistant);
  return wrap;
}

function displayReply(turn) {
  const text = t();
  if ((turn.cards || []).length > 1) return text.several;
  const price = (turn.cards || []).find((card) => card.fields && card.fields.price != null);
  if (price) return fill(text.price_line, { price: price.fields.price, currency: money(price.currency || "USD"), stock: price.fields.stock });
  const days = (turn.facts || []).find((fact) => fact.field === "window_days");
  if (days) return fill(text.returns_line, { days: days.value });
  if (turn.shipment && text.shipment[turn.shipment.availability]) return text.shipment[turn.shipment.availability];
  if ((turn.limitations || []).some((item) => item.code === "no_match" || item.code === "retrieval_miss")) return text.no_match;
  if (turn.route === "clarification") return text.unsupported;
  return text.unsupported;
}

function stepTrace(turn, open) {
  const details = document.createElement("details");
  details.className = "step-trace";
  if (open) details.open = true;
  const summary = document.createElement("summary");
  const skills = (turn.skills || []).map((item) => item.skill_id).join(", ") || t().planner_no;
  summary.textContent = `${t().trace} · ${skills} · ${turn.duration_ms} ${t().duration_suffix}`;
  details.appendChild(summary);
  details.appendChild(traceNodes(turn));
  return details;
}

function productCard(card) {
  const node = document.createElement("article");
  node.className = "product-card";
  node.appendChild(art(card.fields && card.fields.color));
  const copy = document.createElement("div");
  const name = document.createElement("strong");
  name.textContent = card.name || card.product_id;
  const id = document.createElement("div");
  id.className = "note";
  id.textContent = card.product_id;
  const price = document.createElement("div");
  price.className = "price";
  price.textContent = `${card.fields.price} ${money(card.currency || "USD")}`;
  const meta = document.createElement("div");
  meta.className = "note";
  meta.textContent = ["stock", "color", "size", "material"].map((key) => `${t().fields[key]} ${card.fields[key]}`).join(" · ");
  copy.append(name, id, price, meta);
  node.appendChild(copy);
  return node;
}

function openTrace(turn) {
  trace.hidden = false;
  document.querySelector("#open-trace").setAttribute("aria-expanded", "true");
  traceBody.replaceChildren(turn ? traceNodes(turn) : note(t().trace_empty));
}

function closeTrace() {
  trace.hidden = true;
  document.querySelector("#open-trace").setAttribute("aria-expanded", "false");
}

function traceNodes(turn) {
  const root = document.createElement("div");
  addRow(root, t().request, turn.request_id);
  addRow(root, t().duration, `${turn.duration_ms} ${t().duration_suffix}`);
  addRow(root, t().route, `${t().routes[turn.route] || turn.route} · ${turn.route_reason_code || ""}`);
  addRow(root, t().planner, turn.planner_invoked ? t().planner_yes : t().planner_no);
  addRow(root, t().terminal, `${t().terminals[turn.terminal && turn.terminal.status] || ""} · ${(turn.terminal && turn.terminal.reason_code) || ""}`);
  addRow(root, t().model, turn.model_called ? "called" : t().model_no);
  addRow(root, t().gate, fill(t().gate_line, {
    allowed: turn.evidence_gate ? turn.evidence_gate.allowed_count : 0,
    blocked: turn.evidence_gate && turn.evidence_gate.blocked ? turn.evidence_gate.blocked.length : 0,
  }));
  for (const skill of turn.skills || []) {
    const line = document.createElement("p");
    line.textContent = `${t().skill} ${skill.skill_id} · ${skill.status}`;
    root.appendChild(line);
  }
  for (const tool of turn.tools || []) {
    const line = document.createElement("p");
    line.textContent = `${tool.tool} · ${tool.status} · ${t().tool_note}`;
    root.appendChild(line);
  }
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = t().details;
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify({ tools: turn.tools, evidence_gate: turn.evidence_gate, engine: turn.reply_text }, null, 2);
  details.append(summary, pre);
  root.appendChild(details);
  return root;
}

function showNote(text) {
  const node = document.createElement("p");
  node.className = "note";
  node.dataset.role = "notice";
  node.textContent = text;
  transcript.appendChild(node);
}

function addRow(root, label, value) {
  const row = document.createElement("div");
  row.className = "row";
  const name = document.createElement("span");
  name.textContent = label;
  const val = document.createElement("b");
  val.textContent = value == null ? "" : String(value);
  row.append(name, val);
  root.appendChild(row);
}

function note(text) {
  const node = document.createElement("p");
  node.className = "note";
  node.textContent = text;
  return node;
}

function fill(template, values) {
  return String(template).replace(/\{(\w+)\}/g, (_, key) => values[key] == null ? "" : String(values[key]));
}

function money(currency) {
  if (locale === "zh" && currency === "USD") return "美元";
  return currency || "";
}

function art(color) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 88 72");
  const paint = { sand: "#d8c3a5", blue: "#3d6f9a", red: "#b94a48", navy: "#24324a", olive: "#6d7340", steel: "#8d98a3" }[color] || "#0e6e6b";
  const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  body.setAttribute("x", "14");
  body.setAttribute("y", "16");
  body.setAttribute("width", "60");
  body.setAttribute("height", "40");
  body.setAttribute("rx", "12");
  body.setAttribute("fill", paint);
  svg.appendChild(body);
  return svg;
}
