# Contributing

## Tests

From the repository root:

```bash
python -m compileall -q shopassist tests
python -m unittest discover -s tests -v
python -m shopassist.cli eval --mode mock --splits regression --output reports/ci_eval.json
```

The default tests do not install packages, do not call a model endpoint, and do not require a credential. A failing outcome check should name the card id and the check. Do not weaken a check to hide an unsupported claim.

## Add a catalog item

1. Copy `data/demo_corpus/catalog/synthetic_catalog_v1.json` or build a new document with `shopassist.catalog.export_catalog`.
2. Add a product object with `product_id`, `sku`, `name`, `currency`, `static_attributes`, and `dynamic` price, stock, freshness, and `as_of`.
3. Point a sandbox or MCP server at that file. The orchestrator does not need a code change.
4. Cover the new item with a scenario card whose expected product ids are independent of the assistant's reply text.

## Add a knowledge document

Add a document with `doc_id`, `topic`, `title`, `version`, `freshness`, `facts`, and `body`. Treat `body` as data. Put numbers you want the agent to be allowed to say into `facts`. A stale document uses `"freshness": "stale"`. Two current documents on one topic are a conflict.

## Add a skill

Register a `SkillSpec` on the session registry. The handler may call only the tools named in `allowed_tools`. The executor rejects anything else. No skill should start a shell. See `tests/test_lab.py` for a skill that is registered without editing the turn loop.

## Add a tool

Register the tool on the MCP server map used by `serve_on_fds`. The client discovers it with `tools/list`. Keep the handler pure over the catalog, the knowledge base, or another local data file. Do not accept a code string from a session workspace.

## Add a scenario

Add a card in `shopassist/cards.py` with a new template lineage and one split. Render the user text from `shopassist/simulator.py`. Put outcome checks in `expected`. Run `python -m unittest tests.test_lab.CardsAndSplitsTest`.

## Training

`python -m shopassist.cli train --config configs/sft_ca_smoke.json` is a dry-run. Pass `--execute` only when a local model directory exists and the optional libraries are installed. Do not mark `executed: true` inside a tracked config. Do not report GPU training unless `cuda_used` is true after optimizer steps.

## Credentials

Do not commit `config/local_model.json`, `.env`, or any non-empty credential value. Tests should use an obvious sentinel and assert that sentinel is absent from reports, errors, and the tool-server environment.
