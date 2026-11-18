"""Build, validate, and split the demonstration corpus."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from shopassist.cards import REQUIRED_TAGS, all_cards
from shopassist.catalog import catalog_document, export_catalog
from shopassist.harness import evaluate_card
from shopassist.knowledge import export_kb, kb_document
from shopassist.prompts import CA_PROMPT, PA_PROMPT
from shopassist.simulator import SIMULATOR_VERSION, render_turns
from shopassist.versioning import (
    BUILDER_VERSION,
    CATALOG_SNAPSHOT_ID,
    DATA_VERSION,
    KB_VERSION,
    SCHEMA_CA,
    SCHEMA_PA,
)

CORPUS_TIER = "demonstration"


class SplitLeakError(ValueError):
    pass


def assert_split_integrity(cards: list[dict] | None = None) -> None:
    cards = list(cards or all_cards())
    lineage, session, template = {}, {}, {}
    for card in cards:
        for bucket, key in ((lineage, "lineage_id"), (session, "session_id"), (template, "template_id")):
            previous = bucket.get(card[key])
            if previous is not None and previous != card["split"]:
                raise SplitLeakError(f"{key} {card[key]} appears in {previous} and {card['split']}")
            bucket[card[key]] = card["split"]
    regression_sessions = {card["session_id"] for card in cards if card["split"] == "regression"}
    other_sessions = {card["session_id"] for card in cards if card["split"] != "regression"}
    if regression_sessions & other_sessions:
        raise SplitLeakError("regression session leaked into another split")


def coverage_report(cards: list[dict] | None = None) -> dict:
    cards = list(cards or all_cards())
    by_scenario = defaultdict(lambda: {"cards": 0, "splits": set(), "tags": set()})
    tags = set()
    for card in cards:
        row = by_scenario[card["scenario_id"]]
        row["cards"] += 1
        row["splits"].add(card["split"])
        row["tags"].update(card["tags"])
        tags.update(card["tags"])
    return {
        "card_count": len(cards),
        "corpus_tier": CORPUS_TIER,
        "not_production_sufficient": True,
        "missing_tags": sorted(REQUIRED_TAGS - tags),
        "scenarios": {
            key: {"cards": value["cards"], "splits": sorted(value["splits"]), "tags": sorted(value["tags"])}
            for key, value in sorted(by_scenario.items())
        },
        "split_counts": {
            split: sum(1 for card in cards if card["split"] == split)
            for split in ("train", "val", "test", "regression")
        },
    }


def build_examples(cards: list[dict] | None = None) -> list[dict]:
    assert_split_integrity(cards)
    examples = []
    for card in cards or all_cards():
        result = evaluate_card(card)
        if not result["score"]["passed"]:
            raise RuntimeError("reference run failed for " + card["card_id"])
        texts = render_turns(card["turns"])
        for index, turn in enumerate(result["trace"]["turns"]):
            examples.append(_ca_example(card, index, texts[index], turn))
            if turn.get("planner_invoked"):
                examples.append(_pa_example(card, index, texts[index], turn))
    _validate_examples(examples)
    return examples


def _provenance(card: dict, index: int, role: str) -> dict:
    return {
        "scenario_id": card["scenario_id"],
        "template_id": card["template_id"],
        "lineage_id": card["lineage_id"],
        "session_id": card["session_id"],
        "card_id": card["card_id"],
        "turn_index": index,
        "builder": "reference_sandbox_trace",
        "builder_version": BUILDER_VERSION,
        "simulator_version": SIMULATOR_VERSION,
        "user_text_source": "user_simulator",
        "catalog_snapshot_id": CATALOG_SNAPSHOT_ID,
        "kb_version": KB_VERSION,
        "seed": card["seed"],
        "corpus_tier": CORPUS_TIER,
        "split": card["split"],
        "role": role,
        "not_production_sufficient": True,
    }


def _ca_example(card, index, text, turn) -> dict:
    target = {
        "route": turn["route"],
        "route_reason_code": turn["route_reason_code"],
        "must_not_invoke_planner": not turn["planner_invoked"],
        "reply_text": turn["reply_text"],
        "claims": turn["claims"],
        "limitations": turn["limitations"],
        "recovery": turn["recovery"],
    }
    example = {
        "schema_version": SCHEMA_CA,
        "example_id": f"{card['split']}-ca-{card['card_id']}-{index}",
        "role": "ca",
        "phase": "turn",
        "split": card["split"],
        "corpus_tier": CORPUS_TIER,
        "provenance": _provenance(card, index, "ca"),
        "input": {
            "latest_user_turn": text,
            "memory_before": turn.get("memory_before"),
            "available_skills": ["policy_lookup", "product_quote", "product_search", "product_compare", "budget_attribute_validate"],
        },
        "target": target,
        "messages": [
            {"role": "system", "content": CA_PROMPT},
            {"role": "user", "content": json.dumps({"latest_user_turn": text}, sort_keys=True)},
            {"role": "assistant", "content": json.dumps(target, sort_keys=True)},
        ],
    }
    return example


def _pa_example(card, index, text, turn) -> dict:
    target = {
        "plan": turn.get("plan") or [],
        "recovery": turn.get("recovery") or [],
        "limitations": turn.get("limitations") or [],
        "claims": turn.get("claims") or [],
    }
    return {
        "schema_version": SCHEMA_PA,
        "example_id": f"{card['split']}-pa-{card['card_id']}-{index}",
        "role": "pa",
        "phase": "task",
        "split": card["split"],
        "corpus_tier": CORPUS_TIER,
        "provenance": _provenance(card, index, "pa"),
        "input": {
            "latest_user_turn": text,
            "memory_before": turn.get("memory_before"),
            "goal_hint": "complex_task",
        },
        "target": target,
        "messages": [
            {"role": "system", "content": PA_PROMPT},
            {"role": "user", "content": json.dumps({"latest_user_turn": text}, sort_keys=True)},
            {"role": "assistant", "content": json.dumps(target, sort_keys=True)},
        ],
    }


def _validate_examples(examples: list[dict]) -> None:
    ids = set()
    for example in examples:
        if example["example_id"] in ids:
            raise ValueError("duplicate example id")
        ids.add(example["example_id"])
        if example["schema_version"] not in {SCHEMA_CA, SCHEMA_PA}:
            raise ValueError("bad schema")
        if example["provenance"]["user_text_source"] != "user_simulator":
            raise ValueError("user text source")
        if example["corpus_tier"] != CORPUS_TIER:
            raise ValueError("tier")
        blob = json.dumps(example)
        for banned in ("chain_of_thought", "api_key", "hidden_thought"):
            if banned in blob:
                raise ValueError("banned field")
    regression_sessions = {item["provenance"]["session_id"] for item in examples if item["split"] == "regression"}
    for example in examples:
        if example["split"] != "regression" and example["provenance"]["session_id"] in regression_sessions:
            raise SplitLeakError(example["example_id"])


def write_corpus(root: str | Path) -> dict:
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    export_catalog(root / "catalog" / "synthetic_catalog_v1.json")
    export_kb(root / "kb" / "synthetic_kb_v1.json")
    examples = build_examples()
    by_split = defaultdict(list)
    for example in examples:
        by_split[example["split"]].append(example)
    for split, rows in by_split.items():
        _write_jsonl(root / f"{split}.jsonl", rows)
    heldout = root / "regression"
    heldout.mkdir(parents=True, exist_ok=True)
    _write_jsonl(heldout / "heldout_v1.jsonl", by_split["regression"])
    manifest = {
        "data_version": DATA_VERSION,
        "corpus_tier": CORPUS_TIER,
        "not_production_sufficient": True,
        "catalog_snapshot_id": CATALOG_SNAPSHOT_ID,
        "kb_version": KB_VERSION,
        "coverage": coverage_report(),
        "example_counts": {split: len(rows) for split, rows in sorted(by_split.items())},
        "regression_session_ids": sorted({row["provenance"]["session_id"] for row in by_split["regression"]}),
        "regression_lineage_ids": sorted({row["provenance"]["lineage_id"] for row in by_split["regression"]}),
        "role_counts": {
            "ca": sum(1 for row in examples if row["role"] == "ca"),
            "pa": sum(1 for row in examples if row["role"] == "pa"),
        },
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (heldout / "heldout_v1.manifest.json").write_text(
        json.dumps(
            {
                "session_ids": manifest["regression_session_ids"],
                "lineage_ids": manifest["regression_lineage_ids"],
                "example_count": len(by_split["regression"]),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    # Touch canonical documents so callers can hash the in-memory snapshot too.
    manifest["catalog_product_count"] = len(catalog_document()["products"])
    manifest["kb_document_count"] = len(kb_document()["documents"])
    return manifest


def load_split(path: Path, blocked_sessions: set[str] | None = None) -> list[dict]:
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if blocked_sessions and row["provenance"]["session_id"] in blocked_sessions:
            raise SplitLeakError("blocked session in " + str(path))
        rows.append(row)
    return rows


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
    path.write_text(text, encoding="utf-8")
