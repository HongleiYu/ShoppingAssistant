# Configuration and turn streaming

Settings load in this order: built-in defaults, then `SHOPASSIST_CONFIG` or `config/local_shopassist.json` (legacy `config/local_model.json` still loads if the new file is absent), then environment variables, then explicit overrides passed by the caller. Unknown keys fail. `python -m shopassist.cli check-config` prints the effective settings with secrets redacted.

The example file is `config/shopassist.example.json`. Copy it to `config/local_shopassist.json` to change local defaults. Leave secrets blank; put credentials only in environment variables. `config/local_shopassist.json` and `config/local_model.json` are gitignored. The static GitHub Pages build uses built-in defaults only and has no model endpoint and no key field.

A configured communication agent and planner use the OpenAI-compatible chat-completions POST described in the README. The model returns route or planner JSON. Tool results stay on the synthetic snapshots. The evidence gate still checks claims. Invalid model JSON, a missing opt-in, and HTTP 401 fail closed and do not fall back to the mock policy. Live provider accounts were not called by the tests; the configured-path tests use a local stub.

## Turn endings

| Status | Meaning |
| --- | --- |
| COMPLETED | Evidence and current constraints passed an independent check. `verification.passed` is ignored. |
| COMPLETED_NO_MATCH | A clean lookup found nothing. |
| WAITING_USER | The task is paused. The next relevant answer resumes it. A new direct request cancels it. The session stays. |
| FAILED | The lookup failed, including an ungrounded finish or a tool outage. This is not a no-match. |
| STOPPED | A configured step, tool, model, time, token, or repeat budget stopped the task. |
| ESCALATED | Conflicting evidence or a request for a person. |

Mock replies are final-only and labeled `deterministic`. They are not a simulated token stream. A configured endpoint may stream the already validated reply. Deltas are shown only while they match that reply. Any other provider output is replaced by a `buffered_fallback` copy of the validated reply. Product cards are sent once, from evidence, after validation. Routing JSON and tool arguments are never streamed.

`POST /api/sessions/{id}/message` is still one JSON response. `POST /api/sessions/{id}/stream` is the event stream. Reconnecting with the same `request_id` replays the stored events and does not run the tools again.

`loop.wall_clock_seconds` (default 30) is the budget for one user message, including retries. It is not the age of the chat session. Session counters keep accumulating for inspection and do not stop a later message. A resumed task gets a new request budget and does not replay tools that already ran.

`recovery.backoff_base_seconds`, `recovery.backoff_cap_seconds`, `recovery.jitter_ratio`, and `recovery.honor_retry_after` control waits after a transient timeout, 429, or 503. `loop.max_retries_per_step` defaults to 1, so a retryable tool failure may be attempted twice in total. 401, invalid requests, and non-idempotent calls are not retried. The evidence gate always runs; settings cannot turn it off.

`loop.max_plan_steps` bounds the DAG. A plan with more nodes stops as `step_budget` and does not call the planner again. `loop.max_dag_concurrency` defaults to 4. Independent nodes in one wave may overlap on the in-process gateway. A stdio MCP process runs that wave one tool call at a time because one process cannot multiplex responses.

Shipment lookup is a read-only synthetic skill, `shipment_lookup`, over `logistics.lookup`. An empty snapshot says the id is absent from that snapshot. A failed lookup says the status is unavailable. Neither sentence means a carrier confirmed that no shipment exists.

`knowledge.policy_count`, `knowledge.product_count`, `knowledge.seed`, `knowledge.encoder`, `knowledge.embedding_model`, `knowledge.embedding_dim`, `knowledge.top_k`, `knowledge.max_hits`, `knowledge.dedup_cosine`, and `knowledge.score_threshold` control the fictional corpus and the offline index. `max_hits` defaults to 3 and must not exceed `top_k`. Hits below `score_threshold` are dropped and are not padded back up. `dedup_cosine` defaults to 0.92 and collapses near-duplicate chunks after ranking. The hashing encoder rejects a model id. The sentence-transformers encoder requires one and does not fall back. Paths are `knowledge.policy_path`, `knowledge.product_path`, and `knowledge.index_dir`.
