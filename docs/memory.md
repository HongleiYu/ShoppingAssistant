# Session context

User information is three different records.

1. **Profile.** Stable preferences, such as a color the user says they always prefer. It does not expire and it is not a confirmed constraint for the current search.
2. **Recent memory.** Notes extracted from conversations. A note is eligible while `now < created_at + 7 days`. The instant at exactly seven days is expired. Selection is by relevance, then time, and it carries the source session and message id.
3. **Current session.** The model input is the latest 10 stored messages, a summary of the whole session through a watermark, and structured state. A message is one user or assistant record, not a round.

Structured state holds goals, confirmed facts, assumptions, unresolved questions, excluded alternatives, tombstones, and progress. It is updated on every turn. The summary is background. It cannot promote an assumption or override a later correction. If a summary is behind, messages after the watermark that are outside the latest 10 are included as an uncovered tail, or a synchronous catch-up runs when that tail exceeds its budget.

The planner still has its own task state. Handoff copies the current confirmed conditions. The task closes when it finishes. The session state remains.

## Storage

`SHOPASSIST_STORE=local_sqlite` is the offline default. It is SQLite, not MySQL.

`SHOPASSIST_STORE=mysql` uses a MySQL-compatible server and parameterized statements. If that server is down, the process fails and names `local_sqlite` as the offline alternative. It does not pretend MySQL is connected.

`SHOPASSIST_CACHE=local_memory` is an in-process cache. `redis` talks to Redis. If Redis is down and `SHOPASSIST_CACHE_FALLBACK=mysql` while the durable store is MySQL, reads use the MySQL summary and the report says `mysql_fallback`. Any other miss is an error.

Original messages and every summary version are stored durably. The cache can be deleted and the next read restores the summary.

Writes use compare-and-swap on a version number. A stale summary writer is rejected.

## Summary schedule

A refresh is queued after enough new messages since the watermark, or after their rough token estimate crosses a budget. It does not run on every turn merely because the session is long. The mock summarizer is labeled `mock`. A configured summarizer requires its own opt-in endpoint and does not fall back to the mock.

Schema files: `shopassist/storage/schema.py`. Apply them by constructing the store; `migrate()` runs the versioned statements.
