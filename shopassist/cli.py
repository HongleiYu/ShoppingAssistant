"""Command line entrypoints for corpus build, mock eval, and dry-run training."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shopassist.config import ConfigError, load_config, public_capabilities, require_configured_ready
from shopassist.corpus import write_corpus
from shopassist.eval.compare import compare_reports
from shopassist.eval.report import build_report, render_markdown
from shopassist.training import preference_training_status, run_sft

REPO_ROOT = Path(__file__).resolve().parents[1]


def _print(payload: dict) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def cmd_build(args: argparse.Namespace) -> int:
    manifest = write_corpus(args.output)
    _print({"status": "built", "output": args.output, "example_counts": manifest["example_counts"], "corpus_tier": "demonstration"})
    return 0


def cmd_eval(args: argparse.Namespace) -> int:
    if args.mode != "mock":
        try:
            cfg = load_config()
            cfg["mode"] = args.mode
            require_configured_ready(cfg)
        except ConfigError as exc:
            _print(exc.public())
            return 3
        _print(
            {
                "status": "not_run_configured_endpoint_not_called",
                "evaluation_kind": "configured_model",
                "mock_fallback": False,
                "hint": "This command does not call a paid or remote endpoint from the default lab.",
            }
        )
        return 3
    splits = set(args.splits.split(",")) if args.splits else None
    report = build_report(splits)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    markdown = output.with_suffix(".md")
    markdown.write_text(render_markdown(report), encoding="utf-8")
    failed = report["counts"]["cases"] - report["counts"]["passed"]
    _print(
        {
            "status": "pass" if failed == 0 else "fail",
            "evaluation_kind": "mock_demo",
            "output": str(output),
            "markdown": str(markdown),
            "passed": report["counts"]["passed"],
            "cases": report["counts"]["cases"],
            "gpu_training_verified": False,
            "judge_available": False,
        }
    )
    return 0 if failed == 0 else 1


def cmd_compare(args: argparse.Namespace) -> int:
    baseline_path = Path(args.baseline)
    candidate_path = Path(args.candidate)
    if not baseline_path.is_file():
        _print({"status": "blocked_missing_baseline", "passed": False, "path": str(baseline_path)})
        return 2
    if not candidate_path.is_file():
        _print({"status": "blocked_missing_candidate", "passed": False, "path": str(candidate_path)})
        return 2
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
    gates = json.loads(Path(args.gates).read_text(encoding="utf-8"))
    result = compare_reports(baseline, candidate, gates)
    output = Path(args.output) if args.output else None
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        output.with_suffix(".md").write_text(_compare_markdown(result), encoding="utf-8")
    _print({"status": result["status"], "passed": result["passed"], "regressions": len(result["regressions"]), "wins": len(result["wins"])})
    return 0 if result["passed"] else 1


def cmd_train(args: argparse.Namespace) -> int:
    manifest = run_sft(args.config, execute=args.execute, corpus_dir=args.corpus)
    _print(manifest)
    if manifest["status"] == "dry_run_only":
        return 0
    if manifest["status"] in {"not_run_model_unavailable", "not_run_optional_dependency_missing"}:
        return 3
    return 0 if manifest.get("optimizer_steps_executed") else 3


def cmd_quickstart(args: argparse.Namespace) -> int:
    corpus = REPO_ROOT / "data" / "demo_corpus"
    manifest = write_corpus(corpus)
    report_path = Path(args.output)
    report = build_report(None)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path.with_suffix(".md").write_text(render_markdown(report), encoding="utf-8")
    baseline_path = REPO_ROOT / "baselines" / "mock_demo_v1.json"
    compare_status = "baseline_not_present"
    if baseline_path.is_file():
        gates = json.loads((REPO_ROOT / "configs" / "gates_demo.json").read_text(encoding="utf-8"))
        compared = compare_reports(json.loads(baseline_path.read_text(encoding="utf-8")), report, gates)
        compare_status = compared["status"]
    ca = run_sft(REPO_ROOT / "configs" / "sft_ca_smoke.json", execute=False, corpus_dir=corpus)
    pa = run_sft(REPO_ROOT / "configs" / "sft_pa_smoke.json", execute=False, corpus_dir=corpus)
    summary = {
        "status": "pass" if report["counts"]["passed"] == report["counts"]["cases"] else "fail",
        "evaluation_kind": "mock_demo",
        "corpus": str(corpus),
        "example_counts": manifest["example_counts"],
        "report": str(report_path),
        "compare": compare_status,
        "train_ca": ca["status"],
        "train_pa": pa["status"],
        "gpu_training_verified": False,
        "configured_model_called": False,
        "judge_available": False,
        "preference_training": preference_training_status()["status"],
    }
    _print(summary)
    return 0 if summary["status"] == "pass" else 1


def cmd_bench(args: argparse.Namespace) -> int:
    import time

    from shopassist.catalog import default_catalog
    from shopassist.sandbox import SessionSandbox

    catalog = default_catalog()
    started = time.perf_counter()
    digest = []
    for _ in range(50):
        digest.append(len(catalog.search({"category": "drinkware", "max_price": 20, "price_op": "lt", "in_stock": True})["items"]))
    catalog_ms = int((time.perf_counter() - started) * 1000)
    box = SessionSandbox.start(seed=1)
    try:
        started = time.perf_counter()
        result = box.invoke("policy_lookup", {"topic": "returns", "include_stale": False})
        mcp_ms = int((time.perf_counter() - started) * 1000)
        status = result["status"]
    finally:
        box.close()
    _print(
        {
            "component": args.component,
            "catalog_iterations": 50,
            "catalog_elapsed_ms": catalog_ms,
            "catalog_result_checksum": sum(digest),
            "mcp_policy_status": status,
            "mcp_elapsed_ms": mcp_ms,
            "note": "Local timing smoke, not a quality claim.",
        }
    )
    return 0


def cmd_eval_catalog(args: argparse.Namespace) -> int:
    from shopassist.eval.catalog import render_catalog_markdown, run_eval_catalog

    purposes = [item for item in args.purpose.split(",") if item] if args.purpose else None
    report = run_eval_catalog(purposes)
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    output.with_suffix(".md").write_text(render_catalog_markdown(report), encoding="utf-8")
    _print(
        {
            "status": "pass" if report["release_passed"] else "fail",
            "unique_executed": report["unique_executed"],
            "unique_catalog_cases": report["matrix"]["unique_case_count"],
            "membership_sum": report["matrix"]["membership_sum"],
            "missing_capabilities": report["matrix"]["missing_capabilities"],
            "production_kpi": None,
            "output": str(output),
        }
    )
    return 0 if report["release_passed"] else 1


def cmd_check_config(_args: argparse.Namespace) -> int:
    _print(public_capabilities())
    return 0


def _compare_markdown(result: dict) -> str:
    lines = [
        "# Comparison",
        "",
        f"Status: {result['status']}",
        f"Wins: {len(result.get('wins') or [])}",
        f"Regressions: {len(result.get('regressions') or [])}",
        "",
    ]
    for gate in result.get("gates") or []:
        lines.append(f"- {gate['id']}: {gate['status']}")
    lines.append("")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="shopassist")
    sub = parser.add_subparsers(dest="command", required=True)
    build = sub.add_parser("build-corpus")
    build.add_argument("--output", default="data/demo_corpus")
    build.set_defaults(func=cmd_build)
    evaluate = sub.add_parser("eval")
    evaluate.add_argument("--mode", default="mock", choices=["mock", "configured_endpoint"])
    evaluate.add_argument("--splits", default="")
    evaluate.add_argument("--output", default="reports/mock_eval.json")
    evaluate.set_defaults(func=cmd_eval)
    compare = sub.add_parser("compare")
    compare.add_argument("--baseline", required=True)
    compare.add_argument("--candidate", required=True)
    compare.add_argument("--gates", default="configs/gates_demo.json")
    compare.add_argument("--output", default="")
    compare.set_defaults(func=cmd_compare)
    train = sub.add_parser("train")
    train.add_argument("--config", required=True)
    train.add_argument("--execute", action="store_true")
    train.add_argument("--corpus", default="data/demo_corpus")
    train.set_defaults(func=cmd_train)
    quick = sub.add_parser("quickstart")
    quick.add_argument("--output", default="reports/quickstart.json")
    quick.set_defaults(func=cmd_quickstart)
    bench = sub.add_parser("bench")
    bench.add_argument("--component", default="catalog")
    bench.set_defaults(func=cmd_bench)
    catalog = sub.add_parser("eval-catalog")
    catalog.add_argument("--purpose", default="")
    catalog.add_argument("--output", default="reports/eval_catalog.json")
    catalog.set_defaults(func=cmd_eval_catalog)
    check = sub.add_parser("check-config")
    check.set_defaults(func=cmd_check_config)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "compare" and not args.output:
        args.output = ""
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
