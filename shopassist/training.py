"""CA and PA supervised-tuning entrypoints.

Dry-run validates data and writes a manifest. It does not run an optimizer.
Optional libraries are imported only when execution is requested and a local
model directory exists.
"""

from __future__ import annotations

import json
from pathlib import Path

from shopassist.corpus import load_split
from shopassist.versioning import PROMPT_CA, PROMPT_PA

REPO_ROOT = Path(__file__).resolve().parents[1]


class TrainingConfigError(ValueError):
    pass


def load_training_config(path: str | Path) -> dict:
    config = json.loads(Path(path).read_text(encoding="utf-8"))
    required = ["role", "model_path", "adapter_output", "lora", "max_steps", "batch_size", "learning_rate", "device", "scale"]
    missing = [key for key in required if key not in config]
    if missing:
        raise TrainingConfigError("missing:" + ",".join(missing))
    if config["role"] not in {"ca", "pa"}:
        raise TrainingConfigError("role")
    if config["role"] not in str(config["adapter_output"]):
        raise TrainingConfigError("adapter_output_must_name_the_role")
    if config["device"] not in {"cpu", "cuda"}:
        raise TrainingConfigError("device")
    lora = config["lora"]
    if int(lora["r"]) < 1:
        raise TrainingConfigError("lora_rank")
    if config["scale"] == "smoke" and int(config["max_steps"]) > 5:
        raise TrainingConfigError("smoke_steps")
    if config["scale"] == "larger" and int(config["max_steps"]) < 50:
        raise TrainingConfigError("larger_steps")
    if config.get("executed") is True:
        raise TrainingConfigError("config_must_not_claim_training_already_ran")
    return config


def run_sft(config_path: str | Path, *, execute: bool = False, corpus_dir: str | Path | None = None) -> dict:
    config = load_training_config(config_path)
    corpus = Path(corpus_dir) if corpus_dir else REPO_ROOT / "data" / "demo_corpus"
    manifest_path = corpus / "regression" / "heldout_v1.manifest.json"
    blocked = set()
    if manifest_path.is_file():
        blocked = set(json.loads(manifest_path.read_text(encoding="utf-8")).get("session_ids") or [])
    train_path = corpus / "train.jsonl"
    rows = load_split(train_path, blocked) if train_path.is_file() else []
    rows = [row for row in rows if row["role"] == config["role"]]
    manifest = {
        "role": config["role"],
        "scale": config["scale"],
        "dry_run": not execute,
        "execute_requested": execute,
        "optimizer_steps_executed": 0,
        "gpu_training_verified": False,
        "cuda_used": False,
        "adapter_written": False,
        "examples_seen": len(rows),
        "lora": config["lora"],
        "model_path_configured": config["model_path"],
        "model_path_exists": Path(config["model_path"]).exists(),
        "prompt_version": PROMPT_CA if config["role"] == "ca" else PROMPT_PA,
        "corpus_tier": "demonstration",
        "not_production_sufficient": True,
        "status": "dry_run_only",
    }
    if not execute:
        return manifest
    if not manifest["model_path_exists"]:
        manifest["status"] = "not_run_model_unavailable"
        return manifest
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import peft  # noqa: F401
    except ImportError:
        manifest["status"] = "not_run_optional_dependency_missing"
        return manifest
    trained = _run_local_optimizer(config, rows)
    manifest.update(trained)
    manifest["status"] = "optimizer_steps_finished" if trained["optimizer_steps_executed"] else "not_run"
    manifest["gpu_training_verified"] = bool(trained.get("cuda_used"))
    return manifest


def _run_local_optimizer(config: dict, rows: list[dict]) -> dict:
    """Run only when a local model and optional libraries are present.

    This function is not used by the default commands in this environment.
    """
    import torch
    from peft import LoraConfig, get_peft_model
    from transformers import AutoModelForCausalLM, AutoTokenizer

    device_name = "cuda" if config["device"] == "cuda" and torch.cuda.is_available() else "cpu"
    device = torch.device(device_name)
    tokenizer = AutoTokenizer.from_pretrained(config["model_path"], local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(config["model_path"], local_files_only=True)
    lora = config["lora"]
    model = get_peft_model(
        model,
        LoraConfig(
            r=int(lora["r"]),
            lora_alpha=int(lora["alpha"]),
            lora_dropout=float(lora["dropout"]),
            target_modules=list(lora["target_modules"]),
            bias="none",
            task_type="CAUSAL_LM",
        ),
    )
    model.to(device)
    model.train()
    optimizer = torch.optim.AdamW(model.parameters(), lr=float(config["learning_rate"]))
    steps = 0
    if not rows:
        return {"optimizer_steps_executed": 0, "cuda_used": False, "adapter_written": False}
    for step in range(int(config["max_steps"])):
        text = rows[step % len(rows)]["messages"][-1]["content"]
        batch = tokenizer(text, return_tensors="pt", truncation=True, max_length=256)
        batch = {key: value.to(device) for key, value in batch.items()}
        loss = model(**batch, labels=batch["input_ids"]).loss
        loss.backward()
        optimizer.step()
        optimizer.zero_grad(set_to_none=True)
        steps += 1
    output = Path(config["adapter_output"])
    output.mkdir(parents=True, exist_ok=True)
    model.save_pretrained(output)
    return {
        "optimizer_steps_executed": steps,
        "cuda_used": device.type == "cuda",
        "adapter_written": True,
    }


def preference_training_status() -> dict:
    return {
        "implemented": False,
        "status": "roadmap",
        "optimizer_steps_executed": 0,
        "gpu_training_verified": False,
        "note": "Preference and reward training are not implemented.",
    }


def run_preference_training(*args, **kwargs):
    del args, kwargs
    raise NotImplementedError("Preference and reward training is a roadmap item and has no training implementation.")
