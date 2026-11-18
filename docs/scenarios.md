# Scenarios

Cards live in `shopassist/cards.py`. The same list drives the chat gallery and the regression runner. Tags below are the coverage set the tests require.

| Situation | What the reference runtime does |
| --- | --- |
| Thanks, greeting, acknowledgement | Direct answer, no planner, no tool call |
| Unrelated weather or joke | Direct answer, no planner |
| Capability question | Direct answer, no tool |
| Return, warranty, or shipping question | `policy_lookup` through MCP |
| Older return note | Current fact kept; stale note ignored |
| Only a stale note | No numeric claim; limitation `stale_record_ignored` |
| Missing topic | Limitation `retrieval_miss` |
| Two current notes | Limitation `policy_conflict`; no chosen number |
| Care note with instruction-like body | Fact from `facts` only; body is not followed |
| Single current price or stock | `product_quote` |
| Stale lamp price | Quote, then a fresh quote; the stale figure is not claimed |
| Search or recommendation | Planner, search skill, then constraint check |
| "Maybe ceramic" | Stored as an assumption and not used as a filter |
| Compare two items | Quote steps, then compare, with dependencies |
| Budget or color change | New task id, session memory updated, previous task summarized |
| Missing budget or category, or two budgets | Clarification, no tool |
| No catalog match | `no_match`, no invented product |
| Requested item out of stock | Quote, then fallback search of in-stock alternatives |
| Empty tool payload | `no_match` even if the snapshot has a match |
| Timeout | One retry |
| Tool error | Fallback to `catalog.search_cached` |
| Malformed payload | Replan through `catalog.browse_category` |
| Conflicting tool payload | Escalate; no price chosen |
| User asks for a person | Escalate with no planner |

Faults are injected in the tool server from the card profile. The agent sees the tool status, not the card id.
