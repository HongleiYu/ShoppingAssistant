const base = new URL("./", document.baseURI);
const params = new URLSearchParams(location.search);
const transcript = document.querySelector("#transcript");
const chips = document.querySelector("#chips");
const message = document.querySelector("#message");
const send = document.querySelector("#send");
const cancel = document.querySelector("#cancel");
const status = document.querySelector("#status");
const traceBody = document.querySelector("#trace-body");
const scenarios = document.querySelector("#scenarios");

let config = null;
let locale = "en";
let worker = null;
let request = 0;
let active = 0;
let ready = false;
let consult = null;
let entryId = "";
let pendingProduct = "";
let turns = [];
let sessionGen = 0;
let activeScenario = null;
let presentTimer = 0;
let presentGen = 0;
const inflight = new Map();

const t = () => config.ui[locale];

init();

async function init() {
  config = await (await fetch(new URL("demo-config.json", base))).json();
  locale = chosenLocale();
  entryId = chosenProduct();
  applyChrome();
  document.querySelector("#lang-en").addEventListener("click", () => setLocale("en"));
  document.querySelector("#lang-zh").addEventListener("click", () => setLocale("zh"));
  document.querySelector("#open-scenarios").addEventListener("click", () => scenarios.showModal());
  document.querySelector("#close-scenarios").addEventListener("click", () => scenarios.close());
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
    stopPresentation();
    if (active) inflight.delete(active);
    clearProgress();
    active = 0;
    cancel.hidden = true;
    send.disabled = !ready;
    status.textContent = t().cancelled;
    renderTranscript();
  });
  renderTranscript();
  renderTrace();
  bootWorker();
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
  renderTranscript();
  renderTrace();
}

function applyChrome() {
  const text = t();
  document.documentElement.lang = locale === "zh" ? "zh-CN" : "en";
  document.title = locale === "zh" ? config.brand_zh : config.brand;
  document.querySelector("#brand-name").textContent = document.title;
  document.querySelector("#guest").textContent = text.guest;
  document.querySelector("#banner").textContent = text.banner;
  document.querySelector("#langs").setAttribute("aria-label", text.language);
  document.querySelector("#lang-en").setAttribute("aria-pressed", locale === "en" ? "true" : "false");
  document.querySelector("#lang-zh").setAttribute("aria-pressed", locale === "zh" ? "true" : "false");
  document.querySelector("#open-scenarios").textContent = text.scenarios;
  document.querySelector("#scenarios-title").textContent = text.scenarios;
  document.querySelector("#close-scenarios").textContent = text.close_scenarios;
  document.querySelector("#trace-title").textContent = text.trace_title;
  document.querySelector("#trace-panel").setAttribute("aria-label", text.trace_title);
  document.querySelector("#message-label").textContent = text.message_label;
  message.placeholder = text.placeholder;
  message.maxLength = config.max_message_chars;
  document.querySelector("#send").textContent = text.send;
  document.querySelector("#cancel").textContent = text.cancel;
  chips.setAttribute("aria-label", text.shortcuts);
  chips.replaceChildren();
  const prompts = activeScenario ? activeScenario.suggestions[locale] : (config.prompts[locale] || []).map((item) => item.text);
  for (const prompt of prompts) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = prompt;
    button.addEventListener("click", () => submitUser(prompt));
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
    block.dataset.scenario = scenario.id;
    const photos = document.createElement("div");
    photos.className = "scenario-photos";
    for (const productId of scenario.product_ids) {
      const img = productImage(productId, productId);
      if (img) photos.appendChild(img);
    }
    const copy = document.createElement("div");
    copy.className = "scenario-copy";
    const title = document.createElement("h3");
    title.textContent = scenario.purpose[locale];
    const sample = document.createElement("p");
    sample.className = "sample";
    sample.textContent = scenario.samples[locale];
    const limits = document.createElement("p");
    limits.className = "note";
    limits.textContent = scenario.limits[locale];
    copy.append(title, sample, limits);
    const enter = document.createElement("button");
    enter.type = "button";
    enter.className = "enter";
    enter.textContent = t().enter;
    enter.addEventListener("click", () => enterScenario(scenario));
    block.append(photos, copy, enter);
    list.appendChild(block);
  }
}

function enterScenario(scenario) {
  const productId = scenario.product_ids[0];
  stopPresentation();
  sessionGen += 1;
  activeScenario = scenario;
  turns = [];
  consult = null;
  pendingProduct = productId;
  message.value = "";
  inflight.clear();
  active = 0;
  cancel.hidden = true;
  clearProgress();
  scenarios.close();
  applyChrome();
  renderTranscript();
  renderTrace();
  if (!ready) {
    status.textContent = t().boot_runtime;
    return;
  }
  status.textContent = t().working;
  postSession(productId, sessionGen, "reset");
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
    const productId = pendingProduct || entryId;
    pendingProduct = "";
    postSession(productId, sessionGen, "consult");
    return;
  }
  if (data.type === "consult" || data.type === "reset") {
    const meta = inflight.get(data.id);
    inflight.delete(data.id);
    if (!meta || meta.gen !== sessionGen) return;
    finishWait();
    const body = data.payload || {};
    const result = data.type === "reset" ? body.consult : body;
    turns = [];
    if (!result || !result.ok) {
      consult = null;
      renderTranscript();
      renderTrace();
      showNote(t().unknown_product);
      status.textContent = t().ready;
      return;
    }
    consult = result;
    if (!activeScenario && result.product_id) {
      activeScenario = config.scenarios.find((item) => item.id === "consult_price" && item.product_ids[0] === result.product_id) || null;
      applyChrome();
    }
    renderTranscript();
    renderTrace();
    status.textContent = t().ready;
    return;
  }
  if (data.type === "result") {
    const meta = inflight.get(data.id);
    inflight.delete(data.id);
    if (!meta || meta.gen !== sessionGen) return;
    if (!data.turn || data.turn.ok === false) {
      clearProgress();
      finishWait();
      showNote(t().runtime_error);
      return;
    }
    beginPresentation(meta, data.turn);
    return;
  }
  if (data.type === "error") {
    const meta = inflight.get(data.id);
    inflight.delete(data.id);
    if (meta && meta.gen !== sessionGen) return;
    clearProgress();
    finishWait();
    showNote(t().runtime_error);
  }
}

function postSession(productId, gen, kind) {
  request += 1;
  active = request;
  inflight.set(active, { kind, gen, productId });
  send.disabled = true;
  cancel.hidden = false;
  status.textContent = t().working;
  worker.postMessage({ type: kind, id: active, product_id: productId });
}

function submitUser(text) {
  const value = (text ?? message.value).trim();
  if (!ready || !value || active) return;
  message.value = "";
  request += 1;
  active = request;
  inflight.set(active, { kind: "turn", gen: sessionGen, text: value });
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

function renderTranscript() {
  transcript.replaceChildren();
  if (consult && consult.card) {
    const block = document.createElement("article");
    block.className = "bubble assistant";
    const stack = document.createElement("div");
    stack.className = "stack";
    stack.appendChild(welcomeGallery());
    const welcome = document.createElement("div");
    welcome.className = "text";
    welcome.dataset.role = "welcome";
    welcome.textContent = activeScenario ? activeScenario.welcome[locale] : t().welcome;
    if (activeScenario && activeScenario.context_note) {
      const context = document.createElement("p");
      context.className = "note";
      context.dataset.role = "context";
      context.textContent = activeScenario.context_note[locale];
      stack.appendChild(context);
    }
    const note = document.createElement("p");
    note.className = "note";
    note.textContent = t().no_checkout;
    stack.append(welcome, note);
    block.appendChild(stack);
    transcript.appendChild(block);
  }
  for (const item of turns) transcript.appendChild(turnBlock(item));
  transcript.scrollTop = transcript.scrollHeight;
}

function turnBlock(item) {
  const wrap = document.createElement("div");
  wrap.className = "exchange";
  const user = document.createElement("article");
  user.className = "bubble user";
  const said = document.createElement("div");
  said.className = "text";
  said.textContent = item.user;
  user.appendChild(said);
  if (item.pending) {
    wrap.appendChild(user);
    return wrap;
  }
  const assistant = document.createElement("article");
  assistant.className = "bubble assistant";
  const stack = document.createElement("div");
  stack.className = "stack";
  const reply = document.createElement("div");
  reply.className = "text reply";
  const full = displayReply(item.turn);
  reply.textContent = typeof item.revealed === "string" ? item.revealed : full;
  stack.appendChild(reply);
  if (item.revealed == null) {
    for (const card of item.turn.display_cards || []) {
      if (card.fields && card.fields.price != null) stack.appendChild(productCard(card));
    }
  }
  assistant.appendChild(stack);
  wrap.append(user, assistant);
  return wrap;
}

function displayReply(turn) {
  const text = t();
  if (turn.route_reason_code === "unrecognized") return text.unsupported_demo;
  if (turn.route === "clarification" && turn.reply_text) return turn.reply_text;
  if ((turn.cards || []).filter((card) => card.fields && card.fields.price != null).length > 1) return text.several;
  const price = (turn.cards || []).find((card) => card.fields && card.fields.price != null);
  if (price) return fill(text.price_line, { price: price.fields.price, currency: money(price.currency || "USD"), stock: price.fields.stock });
  const days = (turn.facts || []).find((fact) => fact.field === "window_days");
  if (days) return fill(text.returns_line, { days: days.value });
  const knowledge = (turn.skills || []).some((item) => item.skill_id === "knowledge_retrieve");
  const policyFacts = (turn.facts || []).filter((fact) => fact.field && fact.field !== "window_days");
  if (knowledge && policyFacts.length) {
    const rendered = policyFacts.map((fact) => `${fact.field}=${fact.value} (${fact.doc_id || ""})`).join("; ");
    return fill(text.knowledge_line, { facts: rendered });
  }
  if (turn.shipment && text.shipment[turn.shipment.availability]) {
    const facts = turn.shipment.facts || {};
    const extra = ["tracking_number", "logistics_status", "eta"].filter((key) => facts[key]).map((key) => `${key} ${facts[key]}`).join(", ");
    return extra ? `${text.shipment[turn.shipment.availability]} ${extra}` : text.shipment[turn.shipment.availability];
  }
  if ((turn.limitations || []).some((item) => item.code === "no_match" || item.code === "retrieval_miss")) return text.no_match;
  return text.unsupported;
}

function productCard(card) {
  const node = document.createElement("article");
  node.className = "product-card";
  const img = productImage(card.product_id, card.name || card.product_id);
  if (img) node.appendChild(img);
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
  meta.textContent = ["stock", "color", "size", "material"].filter((key) => card.fields[key] != null && card.fields[key] !== "").map((key) => `${t().fields[key]} ${card.fields[key]}`).join(" · ");
  copy.append(name, id, price, meta);
  node.appendChild(copy);
  return node;
}

function productImage(productId, name) {
  const rel = config.images && config.images[productId];
  if (!rel) return null;
  const img = document.createElement("img");
  img.className = "product-photo";
  img.alt = name || productId;
  img.src = new URL(rel, base).href;
  return img;
}

function renderTrace() {
  traceBody.replaceChildren();
  if (!turns.length) {
    traceBody.appendChild(note(t().trace_empty));
    return;
  }
  for (const item of turns) {
    const block = document.createElement("section");
    block.className = "trace-turn";
    const title = document.createElement("h3");
    title.textContent = item.user;
    block.appendChild(title);
    block.appendChild(traceNodes(item.turn));
    traceBody.appendChild(block);
  }
}

function traceNodes(turn) {
  const root = document.createElement("div");
  addRow(root, t().request, turn.request_id);
  addRow(root, t().duration, `${turn.duration_ms} ${t().duration_suffix}`);
  addRow(root, t().route, `${t().routes[turn.route] || turn.route} · ${turn.route_reason_code || ""}`);
  addRow(root, t().planner, turn.planner_invoked ? t().planner_yes : t().planner_no);
  addRow(root, t().terminal, `${t().terminals[turn.terminal && turn.terminal.status] || ""} · ${(turn.terminal && turn.terminal.reason_code) || ""}`);
  addRow(root, t().model, turn.simulated_model ? t().model_simulated : (turn.model_called ? "called" : t().model_no));
  for (const call of turn.model_invocations || []) {
    const line = document.createElement("p");
    line.className = "model-call";
    line.textContent = `${call.agent} ${call.phase} · ${call.adapter || ""} · ${call.network ? "network" : "simulated · no network"}`;
    root.appendChild(line);
  }
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

function welcomeGallery() {
  const wrap = document.createElement("div");
  wrap.className = "welcome-products";
  if (consult.card) wrap.appendChild(productCard(consult.card));
  return wrap;
}

function beginPresentation(meta, turn) {
  const gen = meta.gen;
  stopPresentation();
  presentGen = gen;
  const steps = turn.presentation || [];
  let index = 0;
  const motion = reducedMotion();
  const stepMs = motion ? 0 : Number(config.stream.step_ms) || 0;
  turns.push({ user: meta.text, turn, revealed: "", pending: true });
  renderTranscript();
  renderTrace();
  const streamLabel = t().stream_label;
  showProgress(steps.length ? `${streamLabel} ${progressText(steps[0])}` : streamLabel);
  index = steps.length ? 1 : 0;
  const tick = () => {
    if (gen !== sessionGen || gen !== presentGen) return;
    if (index < steps.length) {
      showProgress(`${streamLabel} ${progressText(steps[index])}`);
      index += 1;
      presentTimer = window.setTimeout(tick, stepMs);
      return;
    }
    streamReply(gen, meta, turn);
  };
  presentTimer = window.setTimeout(tick, stepMs);
}

function streamReply(gen, meta, turn) {
  if (gen !== sessionGen || gen !== presentGen) return;
  const answer = displayReply(turn);
  const item = turns[turns.length - 1];
  if (!item || item.user !== meta.text) return;
  item.pending = false;
  item.revealed = "";
  const motion = reducedMotion();
  const size = motion ? Math.max(answer.length, 1) : Math.max(1, Number(config.stream.chunk_chars) || 24);
  const gap = motion ? 0 : Number(config.stream.chunk_ms) || 0;
  let cursor = 0;
  const pump = () => {
    if (gen !== sessionGen || gen !== presentGen) return;
    cursor = Math.min(answer.length, cursor + size);
    item.revealed = cursor >= answer.length ? null : answer.slice(0, cursor);
    if (item.revealed == null) {
      clearProgress();
      presentTimer = 0;
      renderTranscript();
      renderTrace();
      status.textContent = t().done;
      finishWait();
      return;
    }
    const node = transcript.querySelector(".exchange:last-of-type .reply");
    if (node) node.textContent = item.revealed;
    presentTimer = window.setTimeout(pump, gap);
  };
  pump();
}

function showProgress(text) {
  let node = transcript.querySelector(".progress-live");
  if (!node) {
    node = document.createElement("p");
    node.className = "progress-live";
    node.setAttribute("role", "status");
    transcript.appendChild(node);
  }
  node.textContent = text;
}

function clearProgress() {
  const node = transcript.querySelector(".progress-live");
  if (node) node.remove();
}

function stopPresentation() {
  if (turns.length && (turns[turns.length - 1].pending || typeof turns[turns.length - 1].revealed === "string")) turns.pop();
  presentGen += 1;
  if (presentTimer) window.clearTimeout(presentTimer);
  presentTimer = 0;
}

function reducedMotion() {
  return window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function progressText(step) {
  if (!step) return t().sim_running;
  if (step.kind === "grounded") {
    const bits = (step.facts || []).map((fact) => `${fact.field}=${fact.value}`).join(", ");
    return `${t().validated_facts} ${bits}`;
  }
  if (step.agent === "ca" && step.route === "complex_handoff") return t().sim_ca_handoff;
  if (step.agent === "ca" && step.skill_id) return fill(t().sim_ca_tool, { skill: step.skill_id });
  if (step.agent === "pa") return fill(t().sim_pa, { phase: step.phase || "", skill: step.skill_id || "" }).replace(/\s+/g, " ").trim();
  return t().sim_running;
}

function bootWorker() {
  worker = new Worker(new URL("worker.js", base), { type: "module" });
  worker.onmessage = onWorker;
  worker.postMessage({ type: "boot", indexURL: new URL("vendor/pyodide/", base).href });
}

function fill(template, values) {
  return String(template).replace(/\{(\w+)\}/g, (_, key) => values[key] == null ? "" : String(values[key]));
}

function money(currency) {
  if (locale === "zh" && currency === "USD") return "美元";
  return currency || "";
}
