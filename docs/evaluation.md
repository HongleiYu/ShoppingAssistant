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

## Purposes, capabilities, and splits

These are three different labels.

- **Split** (`train`, `val`, `test`, `regression`, `sealed`) controls training leakage. `regression` and `sealed` sessions are blocked from training.
- **Capability** names the behavior under test, such as routing, planning, retrieval, tools, skills, profile, recent memory, session state, end to end, or workload.
- **Purpose** says why the case is in the suite. The registry is `capability_acceptance`, `release_regression`, `robustness_challenge`, `distributional_sample`, `sealed_generalization`, and `workload_cost`.

A case may belong to more than one purpose. Reports de-duplicate by `case_id`. Do not add membership counts and call that the number of cases.

`python -m shopassist.cli eval-catalog` runs the runtime once per unique case and writes a purpose matrix. Outcome checks are constraints and evidence, not one required tool path. Development challenges stay out of the frozen release set until a new catalog version promotes a case that has a validated expected result.

Distributional rows are synthetic strata. Their weights are null, the weighted aggregate is null, and per-stratum pass rates are unweighted. That is not a production traffic score. `production_kpi` stays null until a deidentified sample file is ingested. The empty fixture is `data/eval/deidentified_samples.example.json`. The importer rejects unlabeled text.

The iteration loop is: failing case, triage, keep it as a development challenge or fix the runtime, promote it into a versioned regression set only after the expected outcome is validated, and leave sealed cases and future real samples out of tuning. Offline scores are not business metrics. Online comparisons are not implemented.

Repeat trials default to one. A deterministic mock agreement is not a significance test, and the report says so.


An intentionally degraded report is built by `degrade_report` and is covered by a test that expects the gates to fail.

GPU training is reported as not verified. Dry-run manifests set `optimizer_steps_executed` to 0.
