# Fictional knowledge index

The tiny fixture documents used by regression stay in `shopassist/builtin_data.py`. The generator below does not edit them.

What already existed:

- `kb.get_policy` and `kb.get_document` read those fixtures by topic or id.
- `kb.search` is a literal substring over topic, title, and body.
- There was no vector index and no semantic embedding model.

## Generate

```bash
python -m shopassist.cli build-knowledge --output data/synthetic_kb
```

Defaults from `knowledge` in `config/shopassist.example.json`: 50 policy articles, 1000 products, seed 0. The articles are a fixed fictional set, so the seed does not change their text. The seed changes product color and the generated price, except the anchor row `syn-p-0003` (`Harbor linen napkin`, 18 USD, stock 4). Every row is marked fictional. There are no storefront URLs.

## Build the index

```bash
python -m shopassist.cli build-index --corpus data/synthetic_kb --output data/kb_index
```

The directory contains `manifest.json`, `chunks.jsonl`, `vectors.f32` (little-endian float32), and `catalog.json`. Policy sections become chunks. Each product is one stable record. A second build with the same inputs writes the same bytes. Building again from a smaller catalog removes the missing ids instead of leaving duplicates.

The default encoder is `hashing_lexical_baseline`. A hit must contain a rare query token, and the score is a hashing cosine. The manifest says `semantic: false` and `score_kind: hashing_cosine`.

To try a local semantic model, set `knowledge.encoder` to `sentence_transformers` and `knowledge.embedding_model` to a local model id, then rebuild. The command fails if that package or model is absent. It does not fall back to hashing. This checkout did not run that model.

Query:

```bash
python -m shopassist.cli retrieve --query "会员积分" --corpus policy
python -m shopassist.cli retrieve --query "harbor linen napkin" --corpus product
```

`top_k` defaults to 5 and `score_threshold` defaults to 0.2. An unknown token returns status `empty` and error code `no_match`. If the encoder dimension, model id, or version does not match the manifest, retrieval raises `index_mismatch`.

## Runtime

`kb policy: ...` and `kb product: ...` call the `knowledge_retrieve` skill. On the local server the tool goes through MCP when `data/kb_index/manifest.json` exists. The static page loads the same index in-process and labels the score as a hashing baseline.

A policy reply cites chunk ids and states only structured facts from current chunks. Disagreement becomes a conflict and no number is chosen. A stale fact is not stated as current. Document text is untrusted, including a note that tells the model to promise a 365 day refund. A product reply quotes the fictional catalog after retrieval. A confirmed budget, or `under N` in the same sentence, drops items that fail the price check. The passage cannot replace that price.
