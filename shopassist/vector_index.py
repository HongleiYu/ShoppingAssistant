"""Persist and query a local knowledge index.

The index is a manifest, a float32 matrix, chunk metadata, and a copy of the
fictional product snapshot used for price and stock checks. It is not an
external vector database.
"""

from __future__ import annotations

import array
import hashlib
import json
import shutil
import sys
from pathlib import Path

from shopassist.catalog import Catalog
from shopassist.embeddings import EmbeddingError, _tokens, cosine, load_encoder
from shopassist.kbgen import CATALOG_SNAPSHOT, POLICY_VERSION

INDEX_FILES = ("manifest.json", "chunks.jsonl", "vectors.f32", "catalog.json")


class IndexMismatch(RuntimeError):
    def __init__(self, code: str, details: dict | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


class VectorIndex:
    def __init__(self, directory: Path, manifest: dict, chunks: list[dict], vectors: list[list[float]], catalog: Catalog):
        self.directory = directory
        self.manifest = manifest
        self.chunks = chunks
        self.vectors = vectors
        self.catalog = catalog
        self.document_frequency: dict[str, int] = {}
        for chunk in chunks:
            for token in set(_tokens(chunk["text"])):
                self.document_frequency[token] = self.document_frequency.get(token, 0) + 1

    def search(
        self,
        encoder,
        query: str,
        *,
        corpus: str | None = None,
        top_k: int = 5,
        threshold: float = 0.0,
        product_id: str | None = None,
        product_ids: list[str] | None = None,
        max_hits: int | None = None,
        dedup_cosine: float = 0.92,
    ) -> dict:
        from shopassist.retrieval import expand_query

        _require_compatible(encoder, self.manifest)
        if corpus not in {None, "", "policy", "product"}:
            raise IndexMismatch("unknown_corpus", {"corpus": corpus})
        rewrite = expand_query(query or "")
        # Score the original wording. Expansion only widens the lexical gate.
        query_vector = encoder.encode([rewrite["original"]])[0]
        gate_tokens = set(_tokens(rewrite["expanded"]))
        cutoff = max(1, int(0.05 * max(1, len(self.chunks))))
        rare_tokens = [token for token in gate_tokens if self.document_frequency.get(token, 0) <= cutoff]
        needed = rare_tokens or list(gate_tokens)
        allowed = _product_scope(product_id, product_ids)
        ranked = []
        filtered_out = 0
        for chunk, vector in zip(self.chunks, self.vectors):
            if corpus and chunk["corpus"] != corpus:
                continue
            if not _chunk_in_scope(chunk, allowed):
                filtered_out += 1
                continue
            # Hash buckets can collide. A hit must share a rare query token,
            # or any query token when every token is common. This is lexical.
            hay = set(_tokens(chunk["text"]))
            if not needed or not any(token in hay for token in needed):
                continue
            score = cosine(query_vector, vector)
            if score < float(threshold):
                continue
            ranked.append((score, chunk, vector))
        ranked.sort(key=lambda item: (-item[0], item[1]["chunk_id"]))
        kept, dropped = _dedup(ranked, float(dedup_cosine))
        cap = max(0, int(top_k))
        if max_hits is not None:
            cap = min(cap, max(0, int(max_hits)))
        chosen = kept[:cap]
        hits = [_hit(score, chunk, encoder, self.manifest) for score, chunk, _vector in chosen]
        status = "ok" if hits else "empty"
        dedup_kind = "semantic_near_duplicate" if encoder.semantic else "lexical_near_duplicate"
        return {
            "status": status,
            "error_code": None if hits else "no_match",
            "hits": hits,
            "query": rewrite["original"],
            "query_rewrite": rewrite,
            "corpus": corpus or "all",
            "product_ids": sorted(allowed) if allowed is not None else [],
            "filtered_other_products": filtered_out if allowed is not None else 0,
            "below_threshold_dropped": True,
            "dedup_dropped": dropped,
            "dedup_kind": dedup_kind,
            "returned": len(hits),
            "encoder_kind": encoder.kind,
            "model_id": encoder.model_id,
            "encoder_version": encoder.version,
            "semantic": bool(encoder.semantic),
            "score_kind": encoder.score_kind,
            "index_version": self.manifest["index_version"],
            "content_hash": self.manifest["content_hash"],
            "dim": self.manifest["dim"],
            "kb_version": POLICY_VERSION,
            "snapshot_id": CATALOG_SNAPSHOT,
            "top_k": int(top_k),
            "max_hits": None if max_hits is None else int(max_hits),
            "threshold": float(threshold),
        }

    def quote(self, product_id: str, *, fresh: bool = False) -> dict:
        return self.catalog.quote(product_id, fresh=fresh)


def build_index(policies: list[dict], products: list[dict], encoder, output: str | Path, *, chunk_chars: int = 700, index_version: str = "kb-index-v1") -> dict:
    chunks = chunk_corpus(policies, products, chunk_chars=chunk_chars)
    ids = [chunk["chunk_id"] for chunk in chunks]
    if len(ids) != len(set(ids)):
        raise IndexMismatch("duplicate_chunk_id", {})
    vectors = encoder.encode([chunk["text"] for chunk in chunks])
    if any(len(vector) != encoder.dim for vector in vectors):
        raise IndexMismatch("embedding_dim_mismatch", {"fallback": False})
    manifest = {
        "index_version": index_version,
        "encoder_kind": encoder.kind,
        "model_id": encoder.model_id,
        "encoder_version": encoder.version,
        "semantic": bool(encoder.semantic),
        "score_kind": encoder.score_kind,
        "dim": encoder.dim,
        "float_endian": "little",
        "chunk_count": len(chunks),
        "policy_documents": len(policies),
        "product_records": len(products),
        "content_hash": _content_hash(chunks, products, encoder, index_version),
        "corpora": ["policy", "product"],
        "not_for_training": True,
        "fictional": True,
    }
    target = Path(output)
    staging = target.parent / (target.name + ".staging")
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    _write_vectors(staging / "vectors.f32", vectors)
    with (staging / "chunks.jsonl").open("w", encoding="utf-8") as handle:
        for chunk in chunks:
            handle.write(json.dumps(chunk, sort_keys=True, ensure_ascii=False) + "\n")
    catalog_doc = {
        "snapshot_id": CATALOG_SNAPSHOT,
        "currency_default": "USD",
        "fictional": True,
        "products": products,
    }
    (staging / "catalog.json").write_text(json.dumps(catalog_doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (staging / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    if target.exists():
        shutil.rmtree(target)
    staging.rename(target)
    return manifest


def open_index(directory: str | Path) -> VectorIndex | None:
    root = Path(directory)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    if sys.byteorder != "little" or manifest.get("float_endian") != "little":
        raise IndexMismatch("index_endian", {"byteorder": sys.byteorder, "file": manifest.get("float_endian")})
    chunks = []
    for line in (root / "chunks.jsonl").read_text(encoding="utf-8").splitlines():
        if line.strip():
            chunks.append(json.loads(line))
    raw = (root / "vectors.f32").read_bytes()
    width = int(manifest["dim"])
    count = int(manifest["chunk_count"])
    expected = count * width * 4
    if len(raw) != expected or len(chunks) != count:
        raise IndexMismatch("index_shape", {"bytes": len(raw), "expected": expected, "chunks": len(chunks)})
    values = array.array("f")
    values.frombytes(raw)
    vectors = [list(values[index * width : (index + 1) * width]) for index in range(count)]
    catalog_doc = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
    catalog = Catalog(catalog_doc["snapshot_id"], catalog_doc["products"])
    stored = manifest.get("content_hash")
    check_products = catalog_doc["products"]

    class _ManifestEncoder:
        pass

    encoder_view = _ManifestEncoder()
    encoder_view.kind = manifest["encoder_kind"]
    encoder_view.model_id = manifest["model_id"]
    encoder_view.version = manifest["encoder_version"]
    encoder_view.semantic = manifest["semantic"]
    encoder_view.score_kind = manifest["score_kind"]
    encoder_view.dim = manifest["dim"]
    actual = _content_hash(chunks, check_products, encoder_view, manifest["index_version"])
    if actual != stored:
        raise IndexMismatch("content_hash_mismatch", {"stored": stored, "actual": actual})
    return VectorIndex(root, manifest, chunks, vectors, catalog)


def configured_index_dir(settings: dict) -> Path | None:
    raw = Path(settings["knowledge"]["index_dir"])
    if not raw.is_absolute():
        from shopassist.settings import REPO_ROOT

        raw = REPO_ROOT / raw
    if (raw / "manifest.json").is_file():
        return raw
    return None


def chunk_corpus(policies: list[dict], products: list[dict], *, chunk_chars: int) -> list[dict]:
    chunks = []
    for document in policies:
        for section in document.get("sections") or []:
            text = _normalize(" ".join([document.get("title") or "", section.get("heading") or "", section.get("text") or ""]))
            pieces = _split(text, chunk_chars)
            for offset, piece in enumerate(pieces, start=1):
                suffix = "" if len(pieces) == 1 else f"-{offset}"
                chunks.append(
                    {
                        "chunk_id": f"{document['doc_id']}#{section['section_id']}{suffix}",
                        "corpus": "policy",
                        "doc_id": document["doc_id"],
                        "section_id": section["section_id"],
                        "product_id": None,
                        "scope": "global",
                        "title": document.get("title"),
                        "topic": document.get("topic"),
                        "freshness": document.get("freshness") or "current",
                        "facts": section.get("facts") or {},
                        "text": piece,
                        "untrusted": True,
                        "fictional": True,
                    }
                )
    for product in products:
        static = product["static_attributes"]
        text = _normalize(
            " ".join(
                [
                    product["name"],
                    product.get("description") or "",
                    static.get("category") or "",
                    static.get("color") or "",
                    static.get("material") or "",
                    product["product_id"],
                ]
            )
        )
        chunks.append(
            {
                "chunk_id": product["product_id"] + "#record",
                "corpus": "product",
                "doc_id": product["product_id"],
                "section_id": "record",
                "product_id": product["product_id"],
                "scope": "product",
                "title": product["name"],
                "topic": static.get("category"),
                "freshness": product["dynamic"].get("freshness") or "current",
                "facts": {},
                "text": text,
                "untrusted": True,
                "fictional": True,
            }
        )
    return chunks


def _split(text: str, limit: int) -> list[str]:
    if len(text) <= limit:
        return [text]
    pieces = []
    start = 0
    while start < len(text):
        pieces.append(text[start : start + limit])
        if start + limit >= len(text):
            break
        start += limit
    return pieces


def _normalize(text: str) -> str:
    return " ".join((text or "").split())


def _product_scope(product_id: str | None, product_ids: list[str] | None) -> set[str] | None:
    allowed = set()
    if product_id:
        allowed.add(str(product_id))
    for item in product_ids or []:
        if item:
            allowed.add(str(item))
    return allowed or None


def _chunk_in_scope(chunk: dict, allowed: set[str] | None) -> bool:
    if allowed is None:
        return True
    scope = chunk.get("scope")
    if scope is None:
        scope = "global" if chunk.get("corpus") == "policy" and not chunk.get("product_id") else "product"
    if scope == "global":
        return True
    return chunk.get("product_id") in allowed


def _dedup(ranked: list[tuple], cosine_threshold: float) -> tuple[list[tuple], int]:
    kept = []
    dropped = 0
    for score, chunk, vector in ranked:
        if any(cosine(vector, previous[2]) >= cosine_threshold for previous in kept):
            dropped += 1
            continue
        kept.append((score, chunk, vector))
    return kept, dropped


def _hit(score: float, chunk: dict, encoder, manifest: dict) -> dict:
    return {
        "score": round(float(score), 4),
        "score_kind": encoder.score_kind,
        "semantic": bool(encoder.semantic),
        "encoder_kind": encoder.kind,
        "model_id": encoder.model_id,
        "encoder_version": encoder.version,
        "index_version": manifest["index_version"],
        "content_hash": manifest["content_hash"],
        "corpus": chunk["corpus"],
        "doc_id": chunk["doc_id"],
        "chunk_id": chunk["chunk_id"],
        "product_id": chunk.get("product_id"),
        "scope": chunk.get("scope") or ("global" if chunk.get("corpus") == "policy" and not chunk.get("product_id") else "product"),
        "section_id": chunk.get("section_id"),
        "title": chunk.get("title"),
        "freshness": chunk.get("freshness"),
        "facts": chunk.get("facts") or {},
        "excerpt": (chunk.get("text") or "")[:240],
        "untrusted": True,
    }


def _write_vectors(path: Path, vectors: list[list[float]]) -> None:
    if sys.byteorder != "little":
        raise IndexMismatch("index_endian", {"byteorder": sys.byteorder})
    blob = array.array("f")
    for vector in vectors:
        blob.extend(float(value) for value in vector)
    path.write_bytes(blob.tobytes())


def _content_hash(chunks: list[dict], products: list[dict], encoder, index_version: str) -> str:
    payload = {
        "chunks": chunks,
        "product_ids": [item["product_id"] for item in products],
        "prices": [item["dynamic"]["price"] for item in products],
        "stocks": [item["dynamic"]["stock"] for item in products],
        "freshness": [item["dynamic"]["freshness"] for item in products],
        "encoder_kind": encoder.kind,
        "model_id": encoder.model_id,
        "encoder_version": encoder.version,
        "dim": encoder.dim,
        "index_version": index_version,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _validate_manifest(manifest: dict) -> None:
    required = ("index_version", "encoder_kind", "model_id", "encoder_version", "dim", "chunk_count", "content_hash", "semantic")
    missing = [key for key in required if key not in manifest]
    if missing:
        raise IndexMismatch("manifest_incomplete", {"missing": missing})


def _require_compatible(encoder, manifest: dict) -> None:
    checks = {
        "encoder_kind": encoder.kind == manifest["encoder_kind"],
        "model_id": encoder.model_id == manifest["model_id"],
        "encoder_version": encoder.version == manifest["encoder_version"],
        "dim": encoder.dim == manifest["dim"],
    }
    bad = [key for key, ok in checks.items() if not ok]
    if bad:
        raise IndexMismatch(
            "index_mismatch",
            {
                "fields": bad,
                "encoder": {
                    "kind": encoder.kind,
                    "model_id": encoder.model_id,
                    "version": encoder.version,
                    "dim": encoder.dim,
                },
                "index": {key: manifest.get(key if key != "encoder_version" else "encoder_version") for key in bad},
                "fallback": False,
            },
        )


def encoder_from_settings(settings: dict):
    try:
        return load_encoder(settings)
    except EmbeddingError:
        raise
