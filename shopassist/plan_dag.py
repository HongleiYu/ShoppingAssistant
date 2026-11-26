"""Validate a one-shot planner DAG. Execution does not call the planner again."""

from __future__ import annotations

ALLOWED_SKILLS = {
    "policy_lookup",
    "product_quote",
    "product_search",
    "product_compare",
    "budget_attribute_validate",
    "shipment_lookup",
    "knowledge_retrieve",
}


def validate_dag(plan, *, max_nodes: int) -> list[str]:
    errors: list[str] = []
    if not isinstance(plan, list) or not plan:
        return ["empty_plan"]
    if len(plan) > int(max_nodes):
        errors.append("too_many_nodes")
    ids: list[str] = []
    for node in plan:
        if not isinstance(node, dict):
            errors.append("node_not_object")
            continue
        step_id = node.get("step_id")
        if not isinstance(step_id, str) or not step_id or step_id in ids:
            errors.append("bad_step_id")
        else:
            ids.append(step_id)
        if node.get("skill_id") not in ALLOWED_SKILLS:
            errors.append("skill_not_allowlisted")
        if not isinstance(node.get("arguments"), dict):
            errors.append("arguments_not_object")
        deps = node.get("depends_on") if "depends_on" in node else []
        if not isinstance(deps, list) or not all(isinstance(item, str) for item in deps):
            errors.append("bad_dependency")
    known = set(ids)
    graph: dict[str, list[str]] = {}
    for node in plan:
        if not isinstance(node, dict):
            continue
        step_id = node.get("step_id")
        deps = list(node.get("depends_on") or [])
        graph[step_id] = deps
        for dep in deps:
            if dep not in known:
                errors.append("missing_dependency")
            if dep == step_id:
                errors.append("self_dependency")
        for ref in _refs(node.get("arguments") or {}):
            if ref not in deps:
                errors.append("output_ref_not_dependency")
    if _cyclic(graph):
        errors.append("cycle")
    return errors


def _refs(value) -> list[str]:
    found = []
    if isinstance(value, str) and value.startswith("$") and len(value) > 1:
        found.append(value[1:].split(".", 1)[0])
    elif isinstance(value, dict):
        for item in value.values():
            found.extend(_refs(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_refs(item))
    return found


def _cyclic(graph: dict[str, list[str]]) -> bool:
    state = {key: 0 for key in graph}

    def visit(node: str) -> bool:
        mark = state.get(node, 0)
        if mark == 1:
            return True
        if mark == 2:
            return False
        state[node] = 1
        for dep in graph.get(node) or []:
            if dep in state and visit(dep):
                return True
        state[node] = 2
        return False

    return any(visit(node) for node in list(graph))
