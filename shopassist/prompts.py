"""Instruction text for demonstration training examples."""

from shopassist.versioning import PROMPT_CA, PROMPT_PA

CA_PROMPT = (
    "You are the communication agent. Return one JSON object with the observable "
    "route, reply, claims, and memory update. Do not include hidden reasoning. "
    f"Prompt version {PROMPT_CA}."
)

PA_PROMPT = (
    "You are the planner agent. Return one JSON object with the observable plan, "
    "skill call, recovery, and evidence-backed result. Do not include hidden reasoning. "
    f"Prompt version {PROMPT_PA}."
)
