"""Text encoders for the offline knowledge index.

The default encoder is a hashing lexical baseline. It is not a trained
semantic embedding model. A sentence-transformers backend is optional and
never silently replaces the baseline.
"""

from __future__ import annotations

import hashlib
import importlib.util
import math
import re

TOKEN = re.compile(r"[a-z0-9]+")
CJK = re.compile(r"[\u4e00-\u9fff]+")


class EmbeddingError(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


class HashingEncoder:
    """Character and word hashing. Label: hashing_lexical_baseline.

    Overlapping tokens raise the cosine. This does not measure meaning, and a
    score from this encoder is not a semantic relevance score.
    """

    kind = "hashing_lexical_baseline"
    semantic = False
    score_kind = "hashing_cosine"

    def __init__(self, dim: int = 128, version: str = "hash-v1"):
        if dim < 8:
            raise EmbeddingError("embedding_dim", {"dim": dim})
        self.dim = int(dim)
        self.version = version
        self.model_id = "char-word-hash-" + self.version

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_normalize(_accumulate(text, self.dim)) for text in texts]


class SentenceTransformerEncoder:
    """Local pretrained encoder. Fails closed when the dependency or model is absent."""

    kind = "sentence_transformers"
    semantic = True
    score_kind = "semantic_cosine"

    def __init__(self, model_id: str, dim: int, version: str = "st-v1"):
        if not model_id:
            raise EmbeddingError("embedding_model_required", {"fallback": False})
        if importlib.util.find_spec("sentence_transformers") is None:
            raise EmbeddingError("semantic_dependency_missing", {"model_id": model_id, "fallback": False})
        from sentence_transformers import SentenceTransformer

        self.model_id = model_id
        self.version = version
        self._model = SentenceTransformer(model_id)
        probe = self._model.encode(["probe"], normalize_embeddings=True)
        width = int(len(probe[0]))
        if width != int(dim):
            raise EmbeddingError(
                "embedding_dim_mismatch",
                {"expected": int(dim), "actual": width, "model_id": model_id, "fallback": False},
            )
        self.dim = int(dim)

    def encode(self, texts: list[str]) -> list[list[float]]:
        rows = self._model.encode(list(texts), normalize_embeddings=True)
        return [[float(value) for value in row] for row in rows]


def load_encoder(settings: dict):
    knowledge = settings["knowledge"] if "knowledge" in settings else settings
    kind = knowledge["encoder"]
    dim = int(knowledge["embedding_dim"])
    version = str(knowledge.get("encoder_version") or "")
    if kind == "hashing_lexical_baseline":
        if knowledge.get("embedding_model"):
            raise EmbeddingError("hashing_encoder_rejects_model_id", {"fallback": False})
        return HashingEncoder(dim=dim, version=version or "hash-v1")
    if kind == "sentence_transformers":
        return SentenceTransformerEncoder(
            str(knowledge.get("embedding_model") or ""),
            dim,
            version=version or "st-v1",
        )
    raise EmbeddingError("unknown_encoder", {"encoder": kind, "fallback": False})


def cosine(left: list[float], right: list[float]) -> float:
    if len(left) != len(right) or not left:
        raise EmbeddingError("vector_width_mismatch", {"left": len(left), "right": len(right)})
    return float(sum(a * b for a, b in zip(left, right)))


def _tokens(text: str) -> list[str]:
    lowered = (text or "").lower()
    found = TOKEN.findall(lowered)
    for run in CJK.findall(text or ""):
        if len(run) == 1:
            found.append(run)
            continue
        found.append(run)
        found.extend(run[index : index + 2] for index in range(len(run) - 1))
    return found


def _accumulate(text: str, dim: int) -> list[float]:
    vector = [0.0] * dim
    for token in _tokens(text):
        digest = hashlib.sha256(token.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "little") % dim
        sign = 1.0 if digest[4] % 2 == 0 else -1.0
        vector[bucket] += sign
    return vector


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]
