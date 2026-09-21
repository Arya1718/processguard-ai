"""TF-IDF retrieval index -- local stand-in for Azure AI Search.

Ranks knowledge-base chunks against a free-text query with scikit-learn's
TF-IDF + cosine similarity. No embeddings API and no LLM involved: this is
classic retrieval math, free and dependency-light for local dev.

AZURE AI SEARCH NOTE: this class stands in for Azure AI Search's vector
index and is swappable for a real embedding-based index later behind the
same interface (index/search) -- callers never change.
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Document:
    """One retrievable unit of knowledge."""

    doc_id: str
    title: str
    section: str
    text: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RetrievedChunk:
    """A ranked search hit: chunk content, score, and source citation."""

    doc_id: str
    title: str
    section: str
    text: str
    score: float
    metadata: dict = field(default_factory=dict)


class RetrievalIndex:
    """TF-IDF vector index over knowledge-base documents.

    AZURE AI SEARCH NOTE: stand-in for Azure AI Search's vector index --
    swappable for a real embedding-based index later behind the same
    index/search interface (see app/rag/__init__.py).
    """

    def __init__(self) -> None:
        self._documents: list[Document] = []
        self._vectorizer = None
        self._matrix = None

    def index(self, documents: list[Document]) -> int:
        """(Re)index the given documents; returns the corpus size."""
        if not documents:
            raise ValueError("Refusing to index an empty corpus")
        self._documents = list(documents)
        from sklearn.feature_extraction.text import TfidfVectorizer

        self._vectorizer = TfidfVectorizer(
            lowercase=True,
            stop_words="english",
            ngram_range=(1, 2),
            sublinear_tf=True,
        )
        self._matrix = self._vectorizer.fit_transform([d.text for d in self._documents])
        return len(self._documents)

    def search(self, query: str, top_k: int = 5) -> list[RetrievedChunk]:
        """Return the top_k ranked chunks for the query."""
        if self._vectorizer is None or self._matrix is None:
            raise RuntimeError("RetrievalIndex.search called before index()")
        if not query.strip():
            return []
        import numpy as np

        query_vec = self._vectorizer.transform([query])
        scores = (self._matrix @ query_vec.T).toarray().ravel()
        order = np.argsort(scores)[::-1]
        results: list[RetrievedChunk] = []
        seen_docs: set[str] = set()
        for idx in order:
            score = float(scores[idx])
            if score <= 0.0 or len(results) >= top_k:
                continue
            doc = self._documents[int(idx)]
            # One chunk per DOCUMENT (its best-scoring section): several
            # sections of the same SOP must not crowd unrelated procedures
            # out of the top-k.
            if doc.doc_id in seen_docs:
                continue
            seen_docs.add(doc.doc_id)
            results.append(
                RetrievedChunk(
                    doc_id=doc.doc_id,
                    title=doc.title,
                    section=doc.section,
                    text=doc.text,
                    score=round(score, 4),
                    metadata=dict(doc.metadata),
                )
            )
        return results

    @property
    def size(self) -> int:
        return len(self._documents)
