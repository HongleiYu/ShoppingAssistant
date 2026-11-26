"""Deterministic query expansion for the local index.

Aliases add lexical tokens. They do not measure meaning. A hashing encoder
still produces a hashing cosine. Only a sentence-transformers encoder is a
semantic embedding, and that backend never silently replaces hashing.
"""

from __future__ import annotations

_ALIASES = {
    "mug": ("drinkware",),
    "cup": ("drinkware",),
    "refund": ("return",),
    "cost": ("price",),
    "warranty": ("guarantee",),
}


def expand_query(text: str) -> dict:
    original = " ".join((text or "").split())
    tokens = original.casefold().split()
    added: list[str] = []
    seen = {token.strip(".,?!") for token in tokens}
    for token in list(seen):
        for alias in _ALIASES.get(token, ()):
            if alias not in seen:
                seen.add(alias)
                added.append(alias)
    expanded = original if not added else (original + " " + " ".join(added)).strip()
    return {
        "original": original,
        "expanded": expanded,
        "added_tokens": added,
        "method": "lexical_alias",
        "semantic": False,
    }
