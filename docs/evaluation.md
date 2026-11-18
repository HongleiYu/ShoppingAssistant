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

An intentionally degraded report is built by `degrade_report` and is covered by a test that expects the gates to fail.

GPU training is reported as not verified. Dry-run manifests set `optimizer_steps_executed` to 0.
