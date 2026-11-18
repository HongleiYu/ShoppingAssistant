# Contracts

## Catalog record

- `product_id`, `sku`, `name`, `currency`
- `static_attributes`: category, color, size, material, warranty months
- `dynamic`: price, stock, freshness (`current` or `stale`), `as_of`
- A stale dynamic record may include `refreshed`, which is used only when the quote asks for `fresh: true`

Search filters accept `category`, `color`, `size`, `material`, `max_price`, `in_stock`, `include_stale`, and `price_op` (`lt` or `lte`). Unknown filter keys are rejected. "Under" and "less than" use `lt`.

## Knowledge record

- `doc_id`, `topic`, `title`, `version`, `freshness`, `updated_at`, `facts`, `body`
- `body` is untrusted data. The reply renderer does not read it.
- `get_policy` returns `ok` for one current document, `conflict` for two current documents, `stale` when every document is stale, and `empty` when the topic is absent.

## MCP

Protocol version `2024-11-05`. Messages use `Content-Length` framing. Methods used: `initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `resources/list`, `resources/read`, `ping`.

Built-in tools:

| Tool | Purpose |
| --- | --- |
| `catalog.search` | Filtered search of supported dynamic records |
| `catalog.search_cached` | Same snapshot, used as a fallback after an error |
| `catalog.browse_category` | Unfiltered category list, used after a malformed search |
| `catalog.quote` | Dynamic price and stock; `fresh` selects a refreshed value |
| `catalog.get_attributes` | Static attributes plus the supported dynamic value |
| `catalog.compare` | Supported records for several ids |
| `kb.get_policy` | Topic lookup |
| `kb.get_document` | One document |
| `kb.search` | Literal topic or title match |

Resources: `catalog://snapshot`, `catalog://name-index`, `kb://documents`, `kb://doc/{doc_id}`. The name index has ids and names, not prices.

The client validates tool payloads. A payload that fails the schema becomes `malformed` and its item list is not trusted. Message size is capped at 1_000_000 bytes.

## Skills (`skills-v1`)

| Skill | Version | Tools |
| --- | --- | --- |
| `product_search` | 1.0.0 | `catalog.search`, `catalog.search_cached`, `catalog.browse_category` |
| `product_quote` | 1.0.0 | `catalog.quote` |
| `product_compare` | 1.0.0 | `catalog.compare`, `catalog.get_attributes` |
| `policy_lookup` | 1.0.0 | `kb.get_policy`, `kb.get_document`, `kb.search` |
| `budget_attribute_validate` | 1.0.0 | none |

## Agent outputs

CA routes: `direct_answer`, `clarification`, `simple_skill`, `complex_handoff`, `human_escalation`.

PA actions: `plan`, `tool_call`, `retry`, `fallback`, `replan`, `finish`, `fail`.

Claims name a field, a value, a product or document id, evidence ids, a record class (`static` or `dynamic`), and freshness. A claim is supported only when an `ok` evidence payload contains that value for that id. Stale prices and conflicting candidates are not supported.

Banned trace keys include `chain_of_thought`, `reasoning`, `scratchpad`, and `api_key`.
