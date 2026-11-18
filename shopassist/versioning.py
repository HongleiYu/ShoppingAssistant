"""Version identifiers for schemas, prompts, tools, and snapshots."""

PACKAGE_VERSION = "0.1.0"
SCHEMA_CA = "ca.v1"
SCHEMA_PA = "pa.v1"
SCHEMA_REPORT = "report.v1"
PROMPT_CA = "ca-prompt-v1"
PROMPT_PA = "pa-prompt-v1"
TOOL_CONTRACT_VERSION = "tools-v1"
MCP_PROTOCOL_VERSION = "2024-11-05"
CATALOG_SNAPSHOT_ID = "synthetic-catalog-v1"
KB_VERSION = "synthetic-kb-v1"
DATA_VERSION = "demo-corpus-v1"
BUILDER_VERSION = "gold-v1"
SIMULATOR_VERSION = "sim-v1"
SKILL_CONTRACT_VERSION = "skills-v1"

BANNED_TRACE_KEYS = frozenset(
    {
        "chain_of_thought",
        "hidden_thought",
        "hidden_reasoning",
        "reasoning",
        "scratchpad",
        "cot",
        "api_key",
        "authorization",
    }
)
