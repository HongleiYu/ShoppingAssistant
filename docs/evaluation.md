# Evaluation

`python -m shopassist.cli eval --mode mock` runs every card through `evaluate_card`. That function renders user text, opens a sandbox, runs the CA and PA, and scores the trace.

Checks include:

- Route and whether the planner ran
- Supported claims against evidence payloads
- Required facts, product ids, and limitation codes
- Recovery strategy after a fault
- Hard constraints using the constraints confirmed on that turn
- Memory after each turn
- Digits and product ids in the reply that are not backed by a supported claim or by the user's own numbers
- No banned reasoning keys
- Token usage left null for the mock, with an explicit reason

These checks are not a full semantic judge. The optional judge hook returns `available: false` and `score: null` unless a later operator wires a local judge. A null score does not pass a gate. The judge gate in `configs/gates_demo.json` is disabled and reported as skipped. Enabling it without a judge fails the gate as unavailable.

`python -m shopassist.cli compare` aligns cases by id. A missing baseline or candidate is `blocked_missing_*` and a non-zero exit, not a pass. Wins and regressions use pass/fail, unsupported claims, hard-constraint violations, and unnecessary planner calls. Latency is recorded and is not a gate.

`evaluation_kind` is `mock_demo` for the default runner. A configured session uses the same card runner, skills, and tool server, with CA and PA decisions taken from the configured endpoints. Invalid model output fails the request. It does not switch to the mock policy. The default CLI eval stays on mock so a normal command does not call an endpoint.

## Suites, tags, and pipeline splits

These are three different labels. A case can sit in more than one suite. Reports de-duplicate by `case_id`. Do not add membership counts and call that the number of cases.

| Suite | When | What it contains | How it changes |
| --- | --- | --- | --- |
| development | During tuning | Current issues, feature variants, faults | Copy an online failure here with its lineage. Add reviewed variants. Do not promote every variant. |
| regression | Before a release | Versioned representative behavior and reviewed important failures. `critical: true` is a flag inside this suite. | Promote one validated case with review metadata, expected predicates, and a new catalog version. |
| online_sample | During or after a gray release, including offline replay of a batch that already exists | Deidentified full sessions and tool evidence, with window, provenance, and sampling metadata | Import a batch. Archive the previous batch. An empty file is not a traffic score. |

Pipeline split (`train`, `val`, `test`, `regression`, `sealed`) is separate. It decides which scenario cards may become supervised-tuning rows. Regression and sealed sessions stay out of training. The eval catalog does not write training rows. Online sessions and promoted cases are marked `training_row: false`.

Tags carried over from the old six purposes: `long_context`, `memory`, `ca`, `pa`, `mcp_tools`, `robustness`, `perf_cost`, `novel_combination`. Finer capability names such as retrieval and profile remain on the case. There is no sealed-generalization suite. `sealed-notebook` stays a regression case with the `novel_combination` tag and the `sealed` split.

v1 names still select cases through `--purpose`:

| v1 name | v2 selection |
| --- | --- |
| capability_acceptance | regression suite |
| release_regression | regression suite, critical only |
| robustness_challenge | development suite, tag `robustness` |
| distributional_sample | synthetic demos only, not the online suite |
| sealed_generalization | regression suite, tag `novel_combination` |
| workload_cost | tag `perf_cost` |

`python -m shopassist.cli eval-catalog` runs the current runtime once per selected case. It does not score a fixture against itself. Repeat trials default to one (`--repeat`). The report does not claim a significance test.

Three release gates, in `configs/release_gates.json`:

1. Every critical regression case passes. A failure here is not cancelled by a gain on another case.
2. The regression pass rate stays within the configured drop from the same catalog version and snapshot, and a selected target issue moves from fail to pass. Unknown stays unknown.
3. Latency or token cost stays inside an explicit budget. Unset budgets are not a pass and are not a measured P95. Mock latency is an offline proxy. Token totals stay null when the runtime does not report them.

Offline gates can mark a build eligible for a gray check. They never set `production_release`. That stays false until a real online batch exists. `production_kpi` and `online_metrics` stay null. The empty fixtures are `data/eval/deidentified_samples.example.json` and `data/eval/online_samples.example.json`.

Commands:

```bash
python -m shopassist.cli eval-catalog --suite development --tag memory --output reports/dev_memory.json
python -m shopassist.cli eval-catalog --suite regression --baseline reports/regression_baseline.json --gates configs/release_gates.json --output reports/regression.json
python -m shopassist.cli eval-import-online --input data/eval/online_samples.example.json --output reports/online_batch.json
python -m shopassist.cli eval-archive-online --input reports/online_batch.json --output reports/online_archive.json
python -m shopassist.cli eval-promote --case mem-long-correction --review data/eval/promotion.example.json --version eval-catalog-v3
```

The checked-in promotion file uses `decision: example_only`, so the promote command stops instead of changing the frozen case. A real review needs `decision: promote`, a reviewer, a time, expected predicates, source refs, and a new version.

Synthetic example already in the regression suite: `mem-long-correction`. The user asks for drinkware under 20, sends several thanks, then sets the budget to 13. The expected predicates are coverage of the whole session, confirmed budget 13, and a tombstone of the previous budget. A new color variant of that session stays in development until its own review. The runtime must show the new budget; the summary does not override it.

Exit codes: 0 when the requested regression check passes, 1 when a gate fails, 2 when a required gate is unknown. A development subset that contains no critical case exits 0 after it finishes, and the report still lists failures.


An intentionally degraded report is built by `degrade_report` and is covered by a test that expects the gates to fail.

GPU training is reported as not verified. Dry-run manifests set `optimizer_steps_executed` to 0.
