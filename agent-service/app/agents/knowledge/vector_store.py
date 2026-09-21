"""Vector store interface -- EXTENSION POINT ONLY (Prompt 2+).

This module defines where retrieval-augmented knowledge (RAG) will plug in.
Prompt 1 ships only a local JSON/file-backed stub standing in for Azure AI
Search. Do NOT build real RAG or embedding logic here yet.
"""
from __future__ import annotations

import abc
import json
import os
from datetime import datetime, timezone


class VectorStore(abc.ABC):
    """Interface for a knowledge index (stand-in for Azure AI Search).

    A real implementation (Azure AI Search or a local vector DB) replaces
    JsonFileVectorStore without changing any agent code.
    """

    @abc.abstractmethod
    async def upsert(self, doc_id: str, text: str, metadata: dict) -> None:
        """Store or update a document."""

    @abc.abstractmethod
    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        """Return the top_k most relevant documents for a query."""


class JsonFileVectorStore(VectorStore):
    """Trivial file-backed stub standing in for Azure AI Search.

    Real semantic search is intentionally NOT implemented -- this only
    persists documents so the interface has a concrete shape to build on.
    """

    def __init__(self, path: str | None = None) -> None:
        self._path = path or os.environ.get(
            "PGAI_VECTORSTORE__PATH", "/tmp/pgai-vectorstore.json"
        )

    def _load(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path, "r", encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save(self, data: dict) -> None:
        os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
        with open(self._path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)

    async def upsert(self, doc_id: str, text: str, metadata: dict) -> None:
        data = self._load()
        data[doc_id] = {
            "text": text,
            "metadata": metadata,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }
        self._save(data)

    async def search(self, query: str, top_k: int = 5) -> list[dict]:
        # PROMPT 2+: replace with real retrieval (embeddings + similarity).
        data = self._load()
        return [
            {"id": k, **v} for k, v in list(data.items())[:top_k]
        ]
