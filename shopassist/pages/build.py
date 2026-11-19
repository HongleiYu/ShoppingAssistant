"""Build the static GitHub Pages site.

The site contains the page, a pinned Pyodide runtime, the shopassist package,
and a prebuilt hashing index. It does not download a model while the page runs.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import urllib.request
from pathlib import Path

from shopassist.embeddings import load_encoder
from shopassist.kbgen import generate_policies, generate_products
from shopassist.settings import load_settings
from shopassist.vector_index import build_index

ROOT = Path(__file__).resolve().parents[2]
STATIC = Path(__file__).resolve().parent / "static"
PIN_PATH = Path(__file__).resolve().parent / "pyodide_pin.json"
PACKAGE = ROOT / "shopassist"


def build_site(output: Path, base: str, *, pyodide_cache: Path | None = None, download: bool = True) -> None:
    if not base.endswith("/"):
        base += "/"
    if output.exists():
        shutil.rmtree(output)
    output.mkdir(parents=True)
    for item in STATIC.iterdir():
        target = output / item.name
        if item.is_dir():
            shutil.copytree(item, target)
        else:
            shutil.copy2(item, target)
    html = (output / "index.html").read_text(encoding="utf-8")
    html = html.replace("__SHOPASSIST_BASE__", base)
    (output / "index.html").write_text(html, encoding="utf-8")
    shutil.copy2(Path(__file__).resolve().parent / "demo_config.json", output / "demo-config.json")
    (output / ".nojekyll").write_text("", encoding="utf-8")
    runtime = {"files": {}}
    for path in sorted(PACKAGE.rglob("*")):
        if "__pycache__" in path.parts or "static" in path.parts:
            continue
        if path.name == "pyodide_pin.json":
            continue
        if path.suffix not in {".py", ".json"}:
            continue
        runtime["files"][path.relative_to(ROOT).as_posix()] = path.read_text(encoding="utf-8")
    (output / "runtime.json").write_text(json.dumps(runtime, ensure_ascii=False), encoding="utf-8")
    _write_index(output / "kb-index")
    if pyodide_cache is not None or download:
        _copy_pyodide(output / "vendor" / "pyodide", pyodide_cache, download)


def _write_index(directory: Path) -> None:
    settings = load_settings(defaults_only=True)
    knowledge = settings["knowledge"]
    encoder = load_encoder(knowledge)
    build_index(
        generate_policies(knowledge["policy_count"], knowledge["seed"]),
        generate_products(knowledge["product_count"], knowledge["seed"]),
        encoder,
        directory,
        chunk_chars=knowledge["chunk_chars"],
        index_version=knowledge["index_version"],
    )


def _copy_pyodide(destination: Path, cache: Path | None, download: bool) -> None:
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8"))
    cache = cache or Path("/tmp") / f"pyodide-{pin['version']}"
    cache.mkdir(parents=True, exist_ok=True)
    destination.mkdir(parents=True, exist_ok=True)
    for name, spec in pin["files"].items():
        source = cache / name
        if not source.is_file():
            if not download:
                raise FileNotFoundError(name)
            url = pin["base_url"] + name
            urllib.request.urlretrieve(url, source)
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != spec["sha256"] or source.stat().st_size != spec["size"]:
            raise RuntimeError("pyodide_pin_mismatch:" + name)
        shutil.copy2(source, destination / name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static shopping demo")
    parser.add_argument("--output", default="site")
    parser.add_argument("--base", default="/ShoppingAssistant/")
    parser.add_argument("--pyodide-cache", default="")
    parser.add_argument("--skip-pyodide", action="store_true")
    args = parser.parse_args(argv)
    cache = Path(args.pyodide_cache) if args.pyodide_cache else None
    build_site(Path(args.output), args.base, pyodide_cache=cache, download=not args.skip_pyodide)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
