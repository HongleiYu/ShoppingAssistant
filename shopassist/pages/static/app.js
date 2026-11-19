const base = new URL("./", document.baseURI);
const transcript = document.querySelector("#transcript");
const prompts = document.querySelector("#prompts");
const catalog = document.querySelector("#catalog");
const traceBody = document.querySelector("#trace-body");
const message = document.querySelector("#message");
const send = document.querySelector("#send");
const cancel = document.querySelector("#cancel");
const status = document.querySelector("#status");
const nav = document.querySelector("#nav");
const trace = document.querySelector("#trace");

let config = null;
let worker = null;
let request = 0;
let active = 0;
let ready = false;
let turns = [];
let selected = null;

const routes = () => config.routes || {};
const terminals = () => config.terminals || {};
const fields = () => config.fields || {};

init();

async function init() {
  config = await (await fetch(new URL("demo-config.json", base))).json();
  document.querySelector("#input-note").textContent = config.input_note || "";
  message.maxLength = config.max_message_chars || 2000;
  message.placeholder = config.composer_placeholder || message.placeholder;
  for (const prompt of config.prompts || []) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "prompt";
    button.innerHTML = `<strong></strong><span></span>`;
    button.querySelector("strong").textContent = prompt.label;
    button.querySelector("span").textContent = prompt.text;
    button.addEventListener("click", () => submit(prompt.text));
    prompts.appendChild(button);
  }
  worker = new Worker(new URL("worker.js", base), { type: "module" });
  worker.onmessage = onWorker;
  worker.postMessage({ type: "boot", indexURL: new URL("vendor/pyodide/", base).href });
}

function onWorker(event) {
  const data = event.data || {};
  if (data.type === "boot") {
    status.textContent = data.stage === "pyodide" ? "正在加载本地 Python 运行时。" : "正在装入合成目录和知识索引。";
    return;
  }
  if (data.type === "ready") {
    ready = true;
    send.disabled = false;
    status.textContent = "可以直接发送。回复整段出现，不会假装逐字生成。";
    renderCatalog(data.catalog || []);
    showEmpty();
    return;
  }
  if (data.type === "progress" && active) {
    status.textContent = (data.milestone && data.milestone.text) || "正在运行这一轮。";
    return;
  }
  if (data.type === "result") {
    if (data.id !== active) return;
    finish();
    if (data.turn && data.turn.ok) addTurn(data.turn);
    else showError(data.turn && data.turn.error);
    return;
  }
  if (data.type === "reset" && data.id === active) {
    finish();
    turns = [];
    selected = null;
    showEmpty();
    traceBody.replaceChildren(note("新对话已清空这一轮的内存目录和索引会话。"));
    return;
  }
  if (data.type === "error") {
    finish();
    showError(data.error || "error");
  }
}

function submit(text) {
  const value = (text ?? message.value).trim();
  if (!ready || !value || active) return;
  message.value = "";
  request += 1;
  active = request;
  send.disabled = true;
  cancel.hidden = false;
  status.textContent = "正在运行这一轮。";
  appendUser(value);
  const pending = document.createElement("p");
  pending.className = "pending";
  pending.id = "pending";
  pending.textContent = "正在运行这一轮。";
  transcript.appendChild(pending);
  transcript.scrollTop = transcript.scrollHeight;
  worker.postMessage({ type: "turn", id: active, text: value });
}

function finish() {
  active = 0;
  send.disabled = !ready;
  cancel.hidden = true;
  document.querySelector("#pending")?.remove();
}

function addTurn(turn) {
  turns.push(turn);
  selected = turns.length - 1;
  const block = document.createElement("article");
  block.className = "bubble assistant";
  const text = document.createElement("div");
  text.className = "text";
  text.textContent = turn.reply_text || "";
  block.appendChild(text);
  const notes = document.createElement("div");
  for (const item of turn.limitations || []) {
    const note = (config.limitation_notes || {})[item.code];
    if (!note) continue;
    const p = document.createElement("p");
    p.className = "warn";
    p.textContent = note;
    notes.appendChild(p);
  }
  if (turn.shipment) {
    const p = document.createElement("p");
    p.className = "warn";
    p.textContent = (config.shipment_notes || {})[turn.shipment.availability] || "";
    notes.appendChild(p);
  }
  block.appendChild(notes);
  if ((turn.cards || []).length > 1 && (turn.skills || []).some((item) => item.skill_id === "product_compare")) {
    block.appendChild(compareTable(turn.cards));
  }
  if ((turn.cards || []).length) {
    const grid = document.createElement("div");
    grid.className = "cards";
    for (const card of turn.cards) grid.appendChild(productCard(card));
    block.appendChild(grid);
  }
  const meta = document.createElement("p");
  meta.className = "meta";
  meta.textContent = `${turn.duration_ms} ms · ${routes()[turn.route] || turn.route} · ${terminals()[turn.terminal?.status] || turn.terminal?.status || ""} · ${turn.request_id}`;
  block.appendChild(meta);
  block.addEventListener("click", () => {
    selected = turns.indexOf(turn);
    renderTrace(turn);
  });
  transcript.appendChild(block);
  transcript.scrollTop = transcript.scrollHeight;
  renderTrace(turn);
  status.textContent = "这一轮已经结束。";
}

function appendUser(text) {
  document.querySelector(".empty")?.remove();
  const block = document.createElement("article");
  block.className = "bubble user";
  const body = document.createElement("div");
  body.className = "text";
  body.textContent = text;
  block.appendChild(body);
  transcript.appendChild(block);
}

function showEmpty() {
  transcript.replaceChildren();
  const block = document.createElement("article");
  block.className = "bubble assistant empty";
  const body = document.createElement("div");
  body.className = "text";
  body.textContent = `${config.empty_title || ""} ${config.empty_body || ""}`.trim();
  block.appendChild(body);
  transcript.appendChild(block);
}

function showError(code) {
  status.textContent = `这一轮没有完成（${code || "error"}）。`;
}

function renderCatalog(items) {
  catalog.replaceChildren();
  for (const item of items) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "quote";
    button.innerHTML = "<strong></strong><span></span>";
    button.querySelector("strong").textContent = item.name;
    button.querySelector("span").textContent = `${item.category} · ${item.color} · 问价格`;
    button.addEventListener("click", () => submit(item.quote_text));
    catalog.appendChild(button);
  }
}

function productCard(card) {
  const node = document.createElement("article");
  node.className = "card";
  node.appendChild(art(card.fields?.category, card.fields?.color));
  const title = document.createElement("h3");
  title.textContent = card.name || card.product_id;
  node.appendChild(title);
  const id = document.createElement("p");
  id.className = "note";
  id.textContent = card.product_id;
  node.appendChild(id);
  if (card.fields?.price != null) {
    const price = document.createElement("p");
    price.className = "price";
    price.textContent = `${card.fields.price} USD`;
    node.appendChild(price);
  }
  const facts = document.createElement("p");
  facts.className = "note";
  facts.textContent = Object.entries(card.fields || [])
    .filter(([key]) => key !== "price")
    .map(([key, value]) => `${fields()[key] || key} ${value}`)
    .join(" · ");
  node.appendChild(facts);
  const source = document.createElement("p");
  source.className = "note";
  source.textContent = "价格和库存来自已核对的证据。名称来自合成快照。";
  node.appendChild(source);
  return node;
}

function compareTable(cards) {
  const table = document.createElement("table");
  table.className = "compare";
  const head = document.createElement("tr");
  head.appendChild(document.createElement("th"));
  for (const card of cards) {
    const cell = document.createElement("th");
    cell.textContent = card.name || card.product_id;
    head.appendChild(cell);
  }
  table.appendChild(head);
  for (const key of ["price", "stock", "color", "size", "material", "warranty_months"]) {
    const row = document.createElement("tr");
    const label = document.createElement("th");
    label.textContent = fields()[key] || key;
    row.appendChild(label);
    for (const card of cards) {
      const cell = document.createElement("td");
      const value = card.fields?.[key];
      cell.textContent = value == null ? "—" : String(value);
      row.appendChild(cell);
    }
    table.appendChild(row);
  }
  return table;
}

function renderTrace(turn) {
  traceBody.replaceChildren();
  addRow("请求编号", turn.request_id);
  addRow("耗时", `${turn.duration_ms} ms（只计 user_message）`);
  addRow("沟通代理", `${routes()[turn.route] || turn.route} · ${turn.route_reason_code || ""}`);
  addRow("规划代理", turn.planner_invoked ? "已进入" : "这一轮没有进入规划代理");
  addRow("终态", `${terminals()[turn.terminal?.status] || turn.terminal?.status} · ${turn.terminal?.reason_code || ""}`);
  addRow("模型", turn.model_called ? "已调用" : "未调用。没有 token 用量。");
  addRow("证据门", `通过 ${turn.evidence_gate?.allowed_count ?? 0}，拦住 ${(turn.evidence_gate?.blocked || []).length}`);
  const skills = document.createElement("div");
  for (const skill of turn.skills || []) {
    const row = document.createElement("p");
    row.className = "hit";
    row.textContent = `技能 ${skill.skill_id} ${skill.version || ""} · ${skill.status}`;
    skills.appendChild(row);
  }
  if (turn.planner_actions?.length) {
    for (const step of turn.planner_actions) {
      const row = document.createElement("p");
      row.className = "hit";
      row.textContent = `规划步骤 ${step.action || ""} ${step.skill_id || ""} ${step.step_id || ""}`;
      skills.appendChild(row);
    }
  }
  traceBody.appendChild(skills);
  for (const tool of turn.tools || []) {
    const row = document.createElement("div");
    row.className = "hit";
    const title = document.createElement("strong");
    title.textContent = `${tool.tool} · ${tool.status} · 进程内合成快照`;
    row.appendChild(title);
    for (const hit of tool.output?.hits || []) {
      const line = document.createElement("p");
      line.textContent = `${hit.corpus} ${hit.chunk_id} · ${hit.score_kind || "score"} ${hit.score} · semantic ${hit.semantic}`;
      row.appendChild(line);
    }
    traceBody.appendChild(row);
  }
  if (turn.recovery?.length) addRow("重试或降级", JSON.stringify(turn.recovery));
  const details = document.createElement("details");
  const summary = document.createElement("summary");
  summary.textContent = "技术细节";
  details.appendChild(summary);
  const pre = document.createElement("pre");
  pre.textContent = JSON.stringify(
    {
      tools: turn.tools,
      evidence_gate: turn.evidence_gate,
      limitations: turn.limitations,
      constraints: turn.constraints,
      context: turn.context,
      token_usage_reason: turn.token_usage_reason,
      mcp_process: turn.mcp_process,
    },
    null,
    2,
  );
  details.appendChild(pre);
  traceBody.appendChild(details);
}

function addRow(label, value) {
  const row = document.createElement("div");
  row.className = "row";
  const name = document.createElement("span");
  name.textContent = label;
  const val = document.createElement("b");
  val.textContent = value == null ? "" : String(value);
  row.append(name, val);
  traceBody.appendChild(row);
}

function note(text) {
  const p = document.createElement("p");
  p.className = "note";
  p.textContent = text;
  return p;
}

function art(category, color) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 120 72");
  const paint = {
    sand: "#d8c3a5",
    blue: "#3d6f9a",
    red: "#b94a48",
    navy: "#24324a",
    olive: "#6d7340",
    steel: "#8d98a3",
    natural: "#e6dcc8",
    pink: "#d9a3b0",
  }[color] || "#0e6e6b";
  const body = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  body.setAttribute("x", "28");
  body.setAttribute("y", category === "apparel" ? "16" : "18");
  body.setAttribute("width", "64");
  body.setAttribute("height", category === "drinkware" ? "40" : "36");
  body.setAttribute("rx", "12");
  body.setAttribute("fill", paint);
  svg.appendChild(body);
  return svg;
}

document.querySelector("#composer").addEventListener("submit", (event) => {
  event.preventDefault();
  submit();
});
message.addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    submit();
  }
});
cancel.addEventListener("click", () => {
  active = 0;
  finish();
  status.textContent = "已取消显示这一轮的结果。运行时如果已经算完，结果会被丢掉。";
});
document.querySelector("#reset").addEventListener("click", () => {
  if (!ready) return;
  request += 1;
  active = request;
  worker.postMessage({ type: "reset", id: active });
});
document.querySelector("#open-nav").addEventListener("click", () => nav.classList.toggle("open"));
document.querySelector("#open-trace").addEventListener("click", () => trace.classList.toggle("open"));
document.querySelector("#close-trace").addEventListener("click", () => trace.classList.remove("open"));
