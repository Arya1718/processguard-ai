"""Retrieval-augmented knowledge plumbing (Prompt 3).

AZURE AI SEARCH NOTE: the TF-IDF RetrievalIndex in retrieval.py is a local
STAND-IN for Azure AI Search's vector index. It is classic retrieval math
(no embeddings API, no LLM), so it stays free and dependency-light for dev.
A real embedding-based index can replace it later behind the same interface
(index/search) without changing any agent code.
"""
