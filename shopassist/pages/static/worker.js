import { loadPyodide } from "./vendor/pyodide/pyodide.mjs";

let pyodide = null;
let ready = false;

self.onmessage = async (event) => {
  const message = event.data || {};
  try {
    if (message.type === "boot") {
      await boot(message.indexURL);
      return;
    }
    if (!ready) {
      post({ type: "error", id: message.id, error: "runtime_not_ready" });
      return;
    }
    if (message.type === "turn") {
      pyodide.globals.set("user_text", message.text);
      const raw = pyodide.runPython(`
import json
def _hook(event):
    progress_js(json.dumps(event, ensure_ascii=False))
json.dumps(APP.turn(user_text, on_progress=_hook), ensure_ascii=False)
`);
      post({ type: "result", id: message.id, turn: JSON.parse(raw) });
      return;
    }
    if (message.type === "consult" || message.type === "reset") {
      pyodide.globals.set("product_id", message.product_id || "");
      const source = message.type === "consult"
        ? "import json\njson.dumps(APP.consult(product_id), ensure_ascii=False)"
        : "import json\njson.dumps(APP.reset(product_id or None), ensure_ascii=False)";
      const raw = pyodide.runPython(source);
      post({ type: message.type, id: message.id, payload: JSON.parse(raw) });
      return;
    }
    if (message.type === "catalog") {
      const raw = pyodide.runPython("import json\njson.dumps(APP.catalog(), ensure_ascii=False)");
      post({ type: "catalog", id: message.id, items: JSON.parse(raw) });
    }
  } catch (error) {
    const messageText = String(error && error.message ? error.message : error);
    console.error(messageText);
    post({
      type: "error",
      id: message.id,
      error: "runtime_error",
      message: messageText.slice(0, 500),
    });
  }
};

async function boot(indexURL) {
  post({ type: "boot", stage: "pyodide" });
  pyodide = await loadPyodide({ indexURL });
  pyodide.globals.set("progress_js", (payload) => {
    post({ type: "progress", milestone: JSON.parse(payload) });
  });
  post({ type: "boot", stage: "sqlite" });
  await pyodide.loadPackage("sqlite3");
  post({ type: "boot", stage: "runtime" });
  const runtime = await (await fetch(new URL("./runtime.json", self.location.href))).json();
  for (const [rel, text] of Object.entries(runtime.files)) {
    const full = "/runtime/" + rel;
    pyodide.FS.mkdirTree(full.slice(0, full.lastIndexOf("/")));
    pyodide.FS.writeFile(full, text);
  }
  await writeIndex();
  pyodide.runPython(`
import sys
sys.path.insert(0, "/runtime")
from shopassist.pages_demo import DemoApp
APP = DemoApp(index_dir="/runtime/kb-index")
`);
  const meta = JSON.parse(pyodide.runPython("import json\njson.dumps(APP.meta(), ensure_ascii=False)"));
  const catalog = JSON.parse(pyodide.runPython("import json\njson.dumps(APP.catalog(), ensure_ascii=False)"));
  ready = true;
  post({ type: "ready", meta, catalog });
}

async function writeIndex() {
  const base = new URL("./kb-index/", self.location.href);
  const names = ["manifest.json", "chunks.jsonl", "catalog.json", "vectors.f32"];
  pyodide.FS.mkdirTree("/runtime/kb-index");
  for (const name of names) {
    const response = await fetch(new URL(name, base));
    if (!response.ok) {
      throw new Error("kb_index_missing:" + name);
    }
    const full = "/runtime/kb-index/" + name;
    if (name.endsWith(".f32")) {
      pyodide.FS.writeFile(full, new Uint8Array(await response.arrayBuffer()));
    } else {
      pyodide.FS.writeFile(full, await response.text());
    }
  }
}

function post(payload) {
  self.postMessage(payload);
}
