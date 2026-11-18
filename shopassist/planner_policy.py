"""Planner decisions. Observable plans only; no hidden reasoning channel."""

from __future__ import annotations

COMPLETE = {"ok", "empty"}
FAULTS = {"timeout", "error", "malformed", "stale", "conflict"}


def filters_from_query(query: dict) -> dict:
    filters = {"in_stock": True, "price_op": query.get("price_op") or "lt"}
    for key in ("category", "color", "size"):
        if query.get(key):
            filters[key] = query[key]
    if query.get("max_price") is not None:
        filters["max_price"] = int(query["max_price"])
    if query.get("material"):
        filters["material"] = query["material"]
    return filters


def initial_plan(task: dict) -> list[dict]:
    goal = task["goal_type"]
    query = task["query"]
    if goal in {"search", "recommend"}:
        return [
            _step("s1", "product_search", [], {"filters": filters_from_query(query), "backend": "primary", "limit": 10}),
            _step("s2", "budget_attribute_validate", ["s1"], {"use_previous_items": True}),
        ]
    if goal == "compare":
        ids = list(query.get("product_ids") or [])
        steps = []
        dep = []
        for index, product_id in enumerate(ids):
            step_id = f"q{index + 1}"
            steps.append(_step(step_id, "product_quote", [], {"product_id": product_id, "fresh": False}))
            dep.append(step_id)
        steps.append(_step("cmp", "product_compare", dep, {"product_ids": ids}))
        return steps
    if goal == "oos":
        product_id = (query.get("product_ids") or [None])[0]
        return [_step("q1", "product_quote", [], {"product_id": product_id, "fresh": False})]
    return [_step("s1", "product_search", [], {"filters": filters_from_query(query), "backend": "primary", "limit": 10})]


def browse_plan(task: dict) -> list[dict]:
    return [
        _step("b1", "product_search", [], {"filters": filters_from_query(task["query"]), "backend": "browse", "limit": 10}),
        _step("b2", "budget_attribute_validate", ["b1"], {"use_previous_items": True}),
    ]


def alternative_plan(task: dict) -> list[dict]:
    return [
        _step("a1", "product_search", [], {"filters": filters_from_query(task["query"]), "backend": "primary", "limit": 10}),
        _step("a2", "budget_attribute_validate", ["a1"], {"use_previous_items": True}),
    ]


class PlanRegistry:
    def __init__(self):
        self._builders = {"search": initial_plan, "recommend": initial_plan, "compare": initial_plan, "oos": initial_plan}

    def register(self, goal_type: str, builder) -> None:
        self._builders[goal_type] = builder

    def build(self, task: dict) -> list[dict]:
        builder = self._builders.get(task.get("goal_type"))
        if builder is None:
            raise KeyError(task.get("goal_type"))
        return builder(task)


def new_planner_state() -> dict:
    return {
        "plan": [],
        "observations": [],
        "retries": {},
        "replanned": False,
        "fallback_used": False,
        "oos_branched": False,
    }


def act(task: dict, state: dict, registry: PlanRegistry | None = None) -> dict:
    registry = registry or PlanRegistry()
    if not state["plan"]:
        plan = registry.build(task)
        state["plan"] = plan
        step = _ready(plan, state)[0]
        return _call("plan", plan, step, None)
    last = state["observations"][-1] if state["observations"] else None
    if last and last["status"] in FAULTS and not last.get("handled"):
        last["handled"] = True
        return _recover(task, state, last)
    if (
        last
        and last["status"] == "ok"
        and task.get("goal_type") == "oos"
        and not state["oos_branched"]
        and last.get("skill_id") == "product_quote"
    ):
        stock = int((last.get("items") or [{}])[0].get("stock") or 0)
        if stock <= 0:
            state["oos_branched"] = True
            plan = alternative_plan(task)
            state["plan"] = plan
            step = _ready(plan, state)[0]
            return _call("fallback", plan, step, {"strategy": "fallback", "because_code": "out_of_stock"})
    ready = _ready(state["plan"], state)
    if ready:
        return _call("tool_call", state["plan"], ready[0], None)
    return _conclude(task, state)


def resolve_arguments(step: dict, state: dict, task: dict) -> dict:
    arguments = dict(step.get("arguments") or {})
    if step["skill_id"] != "budget_attribute_validate":
        return arguments
    items: list[dict] = []
    evidence_id = None
    for dep in step.get("depends_on") or []:
        obs = _latest(state, dep)
        if not obs:
            continue
        items = list(obs.get("items") or [])
        evidence = obs.get("evidence") or []
        if evidence:
            evidence_id = evidence[0].get("evidence_id")
    if evidence_id:
        for item in items:
            item["evidence_ids"] = [evidence_id]
    return {"items": items, "filters": filters_from_query(task["query"]), "evidence_id": evidence_id}


def _limits(state: dict) -> dict:
    supplied = state.get("limits") or {}
    return {
        "max_retries_per_step": int(supplied.get("max_retries_per_step", 1)),
        "max_replans": int(supplied.get("max_replans", 1)),
        "max_fallbacks": int(supplied.get("max_fallbacks", 1)),
    }


def _recover(task, state, last) -> dict:
    status = last["status"]
    step_id = last["step_id"]
    retries = state["retries"].get(step_id, 0)
    limits = _limits(state)
    if status == "timeout":
        if retries < limits["max_retries_per_step"]:
            state["retries"][step_id] = retries + 1
            return _call("retry", state["plan"], _step_by_id(state, step_id), {"strategy": "retry", "because_code": "timeout"})
        return _fail("tool_timeout", escalate=False)
    if status == "error":
        if last.get("skill_id") == "product_search" and state.get("fallback_count", 0) < limits["max_fallbacks"]:
            state["fallback_count"] = state.get("fallback_count", 0) + 1
            state["fallback_used"] = True
            step = _step_by_id(state, step_id)
            arguments = dict(step.get("arguments") or {})
            arguments["backend"] = "cached"
            patched = dict(step)
            patched["arguments"] = arguments
            return _call("fallback", state["plan"], patched, {"strategy": "fallback", "because_code": "error"})
        return _fail("tool_error", escalate=False)
    if status == "malformed":
        if state.get("replan_count", 0) < limits["max_replans"] and task.get("goal_type") in {"search", "recommend"}:
            state["replan_count"] = state.get("replan_count", 0) + 1
            state["replanned"] = True
            plan = browse_plan(task)
            state["plan"] = plan
            return _call("replan", plan, plan[0], {"strategy": "replan", "because_code": "malformed"})
        return _fail("malformed_evidence", escalate=False)
    if status == "stale":
        if retries < limits["max_retries_per_step"] and last.get("skill_id") == "product_quote":
            state["retries"][step_id] = retries + 1
            step = _step_by_id(state, step_id)
            arguments = dict((step or {}).get("arguments") or last.get("arguments") or {})
            arguments["fresh"] = True
            patched = dict(step or {"step_id": step_id, "skill_id": "product_quote", "depends_on": []})
            patched["arguments"] = arguments
            return _call("retry", state["plan"], patched, {"strategy": "retry", "because_code": "stale"})
        return _fail("stale_unresolved", escalate=False)
    if status == "conflict":
        return _fail("conflicting_evidence", escalate=True, recovery={"strategy": "escalate_human", "because_code": "conflict"})
    return _fail("tool_error", escalate=False)


def _conclude(task, state) -> dict:
    items = []
    requested = None
    for obs in state["observations"]:
        if obs.get("skill_id") == "product_quote" and obs.get("items"):
            requested = obs["items"][0]
        if obs.get("status") == "ok" and obs.get("skill_id") != "product_quote":
            items = list(obs.get("items") or [])
        elif obs.get("status") == "ok" and obs.get("skill_id") == "product_quote" and task.get("goal_type") != "oos":
            items = list(obs.get("items") or [])
    if task.get("goal_type") == "oos":
        requested_id = None if requested is None else requested.get("product_id")
        items = [item for item in items if item.get("product_id") != requested_id]
        if not items:
            return _fail("no_match", escalate=False, limitations=["requested_out_of_stock", "no_match"])
        finished = _finish(items, ["requested_out_of_stock"])
        finished["result"]["requested_name"] = None if requested is None else requested.get("name")
        return finished
    if not items:
        return _fail("no_match", escalate=False)
    return _finish(items, [])


def _finish(items, limitation_codes) -> dict:
    return {
        "action": "finish",
        "plan": None,
        "skill_call": None,
        "verification": {"passed": True, "issues": []},
        "result": {
            "items": items,
            "limitations": [{"code": code} for code in limitation_codes],
            "summary": "finished",
        },
        "ask": None,
        "failure": None,
        "recovery": None,
    }


def _fail(reason: str, *, escalate: bool, limitations: list[str] | None = None, recovery=None) -> dict:
    codes = limitations or [reason if reason != "conflicting_evidence" else "conflicting_evidence"]
    if reason == "no_match" and "no_match" not in codes:
        codes.append("no_match")
    return {
        "action": "fail",
        "plan": None,
        "skill_call": None,
        "verification": {"passed": False, "issues": [reason]},
        "result": None,
        "ask": None,
        "failure": {
            "reason_code": reason,
            "limitations": [{"code": code} for code in codes],
            "escalate": escalate,
        },
        "recovery": recovery
        or ({"strategy": "escalate_human", "because_code": "conflict"} if escalate else None),
    }


def _ready(plan: list[dict], state: dict) -> list[dict]:
    done = _completed(state)
    ready = []
    for step in plan:
        if step["step_id"] in done:
            continue
        if all(dep in done for dep in step.get("depends_on") or []):
            ready.append(step)
    return ready


def _completed(state: dict) -> set[str]:
    latest = {}
    for obs in state["observations"]:
        latest[obs["step_id"]] = obs
    return {step_id for step_id, obs in latest.items() if obs.get("status") in COMPLETE}


def _latest(state: dict, step_id: str):
    found = None
    for obs in state["observations"]:
        if obs.get("step_id") == step_id:
            found = obs
    return found


def _step_by_id(state, step_id):
    for step in state["plan"]:
        if step["step_id"] == step_id:
            return step
    return None


def _step(step_id, skill_id, depends_on, arguments) -> dict:
    return {
        "step_id": step_id,
        "depends_on": list(depends_on),
        "kind": "skill",
        "skill_id": skill_id,
        "arguments": arguments,
        "intent": skill_id,
    }


def _call(action, plan, step, recovery) -> dict:
    return {
        "action": action,
        "plan": plan,
        "skill_call": {
            "step_id": step["step_id"],
            "skill_id": step["skill_id"],
            "arguments": step.get("arguments") or {},
        },
        "verification": None,
        "result": None,
        "ask": None,
        "failure": None,
        "recovery": recovery,
    }


def plan_is_acyclic(plan: list[dict]) -> bool:
    ids = {step["step_id"] for step in plan}
    graph = {step["step_id"]: list(step.get("depends_on") or []) for step in plan}
    if any(dep not in ids for deps in graph.values() for dep in deps):
        return False
    visiting, visited = set(), set()

    def walk(node: str) -> bool:
        if node in visiting:
            return False
        if node in visited:
            return True
        visiting.add(node)
        for dep in graph[node]:
            if not walk(dep):
                return False
        visiting.remove(node)
        visited.add(node)
        return True

    return all(walk(node) for node in graph)
