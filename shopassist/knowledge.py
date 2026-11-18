"""Knowledge-base module. Document bodies are data, never instructions."""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from shopassist.builtin_data import DOCUMENTS, KB_VERSION


class KnowledgeBase:
    def __init__(self, version: str, documents: list[dict]):
        self.version = version
        self.documents = documents

    def by_topic(self, topic: str) -> list[dict]:
        rows = [deepcopy(doc) for doc in self.documents if doc["topic"] == topic]
        rows.sort(key=lambda doc: (doc["doc_id"], doc["version"]))
        return rows

    def get_document(self, doc_id: str) -> dict:
        for doc in self.documents:
            if doc["doc_id"] == doc_id:
                return {
                    "status": "ok",
                    "kb_version": self.version,
                    "documents": [deepcopy(doc)],
                    "stale_documents": [],
                    "topic": doc["topic"],
                }
        return {
            "status": "empty",
            "kb_version": self.version,
            "documents": [],
            "stale_documents": [],
            "topic": "",
            "doc_id": doc_id,
        }

    def get_policy(self, topic: str, *, include_stale: bool = False) -> dict:
        rows = self.by_topic(topic)
        current = [doc for doc in rows if doc["freshness"] == "current"]
        stale = [doc for doc in rows if doc["freshness"] == "stale"]
        base = {"kb_version": self.version, "topic": topic, "stale_documents": []}
        if not rows:
            return {**base, "status": "empty", "documents": []}
        if len(current) >= 2:
            return {**base, "status": "conflict", "documents": current}
        if len(current) == 1:
            return {
                **base,
                "status": "ok",
                "documents": current,
                "stale_documents": stale if include_stale else [],
            }
        return {**base, "status": "stale", "documents": stale}

    def search(self, query: str) -> dict:
        needle = (query or "").strip().lower()
        found = []
        for doc in self.documents:
            hay = " ".join([doc["topic"], doc["title"], doc["body"]]).lower()
            if needle and needle in hay:
                found.append(
                    {
                        "doc_id": doc["doc_id"],
                        "topic": doc["topic"],
                        "title": doc["title"],
                        "freshness": doc["freshness"],
                        "version": doc["version"],
                    }
                )
        found.sort(key=lambda row: row["doc_id"])
        return {
            "status": "ok" if found else "empty",
            "kb_version": self.version,
            "documents": found,
            "query": query,
        }


def default_kb() -> KnowledgeBase:
    return KnowledgeBase(KB_VERSION, deepcopy(DOCUMENTS))


def kb_document() -> dict:
    return {"kb_version": KB_VERSION, "documents": deepcopy(DOCUMENTS)}


def load_kb(path: str | Path | None = None) -> KnowledgeBase:
    if path is None:
        return default_kb()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    return KnowledgeBase(data["kb_version"], data["documents"])


def export_kb(path: str | Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(kb_document(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
