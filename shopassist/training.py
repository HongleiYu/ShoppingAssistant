"""CA and PA supervised-tuning entrypoints.

Dry-run validates data and does not run an optimizer.
`backend: local_random` runs a tiny CPU optimizer on a generated model.
The optional pretrained-model path imports its libraries only when requested.
"""

from __future__ import annotations

import json
import math
import random
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
    backend = config.get("backend") or "hf_lora"
    if backend not in {"hf_lora", "local_random"}:
        raise TrainingConfigError("backend")
    config["backend"] = backend
    return config


def run_sft(config_path: str | Path, *, execute: bool = False, corpus_dir: str | Path | None = None) -> dict:
    config = load_training_config(config_path)
    corpus = Path(corpus_dir) if corpus_dir else REPO_ROOT / "data" / "demo_corpus"
    manifest_path = corpus / "regression" / "heldout_v1.manifest.json"
    blocked = set()
    if manifest_path.is_file():
        blocked = set(json.loads(manifest_path.read_text(encoding="utf-8")).get("session_ids") or [])
    corpus_manifest = corpus / "manifest.json"
    if corpus_manifest.is_file():
        meta = json.loads(corpus_manifest.read_text(encoding="utf-8"))
        blocked.update(meta.get("sealed_session_ids") or [])
        blocked.update(meta.get("regression_session_ids") or [])
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
    if config.get("backend") == "local_random":
        trained = _run_local_random(config, rows)
        manifest.update(trained)
        manifest["status"] = "optimizer_steps_finished" if trained["optimizer_steps_executed"] else "not_run"
        manifest["gpu_training_verified"] = False
        manifest["quality_validation"] = False
        manifest["backend"] = "local_random"
        return manifest
    if not manifest["model_path_exists"]:
        manifest["status"] = "not_run_model_unavailable"
        manifest["backend"] = "hf_lora"
        return manifest
    try:
        import torch  # noqa: F401
        import transformers  # noqa: F401
        import peft  # noqa: F401
    except ImportError:
        manifest["status"] = "not_run_optional_dependency_missing"
        manifest["backend"] = "hf_lora"
        return manifest
    trained = _run_local_optimizer(config, rows)
    manifest["backend"] = "hf_lora"
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


def _encode(text: str, vocab: int = 64) -> list[int]:
    values = [ord(ch) % vocab for ch in text[:48]]
    return values or [1]


def _embed(ids: list[int], dim: int) -> list[float]:
    vector = [0.0] * dim
    for index, token in enumerate(ids):
        vector[token % dim] += 1.0 / (index + 1)
    return vector


def _forward(features: list[float], factors, coefficients: list[float]) -> tuple[float, list[float]]:
    hidden = []
    for col in range(len(coefficients)):
        hidden.append(sum(features[row] * factors[row][col] for row in range(len(features))))
    prediction = sum(hidden[col] * coefficients[col] for col in range(len(coefficients)))
    return prediction, hidden


def predict_local(adapter_dir: str | Path, text: str) -> float:
    adapter = json.loads((Path(adapter_dir) / "adapter.json").read_text(encoding="utf-8"))
    spec = json.loads((Path(adapter["model_spec"])).read_text(encoding="utf-8"))
    features = _embed(_encode(text, int(spec["vocab_size"])), int(spec["dim"]))
    prediction, _hidden = _forward(features, adapter["factors"], adapter["coefficients"])
    return prediction


def _run_local_random(config: dict, rows: list[dict]) -> dict:
    """Train a generated tiny low-rank model on CPU. This is not a quality run."""
    role = config["role"]
    seed = int(config.get("seed") or 7) + (3 if role == "pa" else 0)
    rank = int(config["lora"]["r"])
    dim = 8
    vocab = 64
    rng = random.Random(seed)
    factors = [[rng.uniform(-0.1, 0.1) for _ in range(rank)] for _ in range(dim)]
    coefficients = [rng.uniform(-0.1, 0.1) for _ in range(rank)]
    velocity_factors = [[0.0 for _ in range(rank)] for _ in range(dim)]
    velocity_coefficients = [0.0 for _ in range(rank)]
    texts = [row["messages"][-1]["content"] for row in rows] or [role]
    probe = texts[0]
    features = _embed(_encode(probe, vocab), dim)
    initial_prediction, _hidden = _forward(features, factors, coefficients)
    steps = 0
    learning_rate = float(config["learning_rate"])
    momentum = 0.9
    for step in range(int(config["max_steps"])):
        ids = _encode(texts[step % len(texts)], vocab)
        features = _embed(ids, dim)
        target = (ids[-1] + 1) / vocab
        prediction, hidden = _forward(features, factors, coefficients)
        grad = 2.0 * (prediction - target)
        next_coefficients = []
        for col, value in enumerate(coefficients):
            grad_coefficient = grad * hidden[col]
            velocity_coefficients[col] = momentum * velocity_coefficients[col] + grad_coefficient
            next_coefficients.append(value - learning_rate * velocity_coefficients[col])
        next_factors = []
        for row in range(dim):
            next_row = []
            for col in range(rank):
                grad_factor = grad * coefficients[col] * features[row]
                velocity_factors[row][col] = momentum * velocity_factors[row][col] + grad_factor
                next_row.append(factors[row][col] - learning_rate * velocity_factors[row][col])
            next_factors.append(next_row)
        factors = next_factors
        coefficients = next_coefficients
        steps += 1
    final_prediction, _hidden = _forward(_embed(_encode(probe, vocab), dim), factors, coefficients)
    model_dir = Path(config["model_path"])
    model_dir.mkdir(parents=True, exist_ok=True)
    spec = {
        "kind": "random_tiny",
        "vocab_size": vocab,
        "dim": dim,
        "rank": rank,
        "seed": seed,
        "role": role,
        "tokenizer": "generated-byte-mod",
    }
    spec_path = model_dir / "model_spec.json"
    spec_path.write_text(json.dumps(spec, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (model_dir / "tokenizer.json").write_text(
        json.dumps({"kind": "generated-byte-mod", "vocab_size": vocab}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output = Path(config["adapter_output"])
    output.mkdir(parents=True, exist_ok=True)
    adapter = {
        "role": role,
        "rank": rank,
        "factors": factors,
        "coefficients": coefficients,
        "steps": steps,
        "model_spec": str(spec_path),
        "optimizer": "momentum_sgd",
    }
    (output / "adapter.json").write_text(json.dumps(adapter) + "\n", encoding="utf-8")
    (output / "trainer.json").write_text(
        json.dumps(
            {
                "backend": "local_random",
                "optimizer_steps_executed": steps,
                "initial_prediction": initial_prediction,
                "final_prediction": final_prediction,
                "gpu_training_verified": False,
                "quality_validation": False,
                "role": role,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    reloaded = predict_local(output, probe)
    if not math.isfinite(reloaded):
        raise TrainingConfigError("local_random_prediction_not_finite")
    return {
        "optimizer_steps_executed": steps,
        "cuda_used": False,
        "adapter_written": True,
        "reloaded": abs(reloaded - final_prediction) < 1e-9,
        "weight_delta_nonzero": abs(final_prediction - initial_prediction) > 1e-8,
        "initial_prediction": initial_prediction,
        "final_prediction": final_prediction,
        "backend": "local_random",
        "quality_validation": False,
        "model_path_exists": True,
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
