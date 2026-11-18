# Data

The demonstration corpus is built by `python -m shopassist.cli build-corpus --output data/demo_corpus`.

User text comes from `shopassist/simulator.py`. The simulator formats scenario slots. It does not import the planner, the catalog prices, or a gold target.

Each example records provenance: scenario, template, lineage, session, seed, snapshot ids, simulator version, and `user_text_source = user_simulator`. `corpus_tier` is `demonstration` and `not_production_sufficient` is true.

## Splits

Splits are assigned on the scenario card, not by a random cut after the fact.

- Every `lineage_id` belongs to one split.
- Every `template_id` belongs to one split.
- Every `session_id` belongs to one split.
- Regression session ids are rejected if they appear in the training file.

`data/demo_corpus/regression/heldout_v1.jsonl` is the held-out export. Training dry-run loads `train.jsonl` and refuses those session ids.

CA and PA examples are different records. A direct answer does not create a PA example. A planner turn creates both.

## What the labels are

Labels are the observable route, plan, claims, limitations, and recovery from a reference run in the sandbox. They are not a hidden chain of thought. Evaluation does not score a candidate by pasting these labels back in as the model's output. The regression runner starts a fresh sandbox and scores the new trace against the card's `expected` block.

`expected` lists routes, product ids, facts, limitation codes, and recovery strategies. It is not sent to the chat UI.
