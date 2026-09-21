"""Unit tests for the RetrievalIndex (Prompt 3).

Proves the TF-IDF stand-in actually discriminates between documents: a query
about the combined "flow drop + vibration" signature must rank SOP-COOL-014
above the unrelated SOPs. No network, no stack required.
"""
from __future__ import annotations

import pytest

from app.rag.corpus import load_corpus
from app.rag.retrieval import Document, RetrievalIndex


@pytest.fixture(scope="module")
def index() -> RetrievalIndex:
    index = RetrievalIndex()
    index.index(load_corpus())
    return index


def test_index_loads_the_seed_corpus(index: RetrievalIndex) -> None:
    # 4 SOP files, sectioned into chunks -- at least 12 documents expected.
    assert index.size >= 12


def test_flow_and_vibration_query_ranks_sop_cool_014_first(index: RetrievalIndex) -> None:
    results = index.search("flow drop and vibration on cooling water pump", top_k=12)
    assert results, "expected hits for the pump signature query"
    assert results[0].doc_id == "SOP-COOL-014"
    # The unrelated SOPs must rank below it wherever they appear.
    ids = [r.doc_id for r in results]
    unrelated = [i for i in ("SOP-DOSE-007", "SOP-CHEM-021") if i in ids]
    assert unrelated, "expected the unrelated SOPs to appear somewhere in the ranking"
    assert all(ids.index("SOP-COOL-014") < ids.index(d) for d in unrelated)


def test_chemistry_query_ranks_the_chem_sop(index: RetrievalIndex) -> None:
    results = index.search("ph excursion conductivity dosing chemistry basin", top_k=5)
    assert results
    assert results[0].doc_id in ("SOP-CHEM-021", "SOP-DOSE-007")
    assert results[0].doc_id != "SOP-COOL-014"


def test_temperature_only_query_prefers_temp_sop(index: RetrievalIndex) -> None:
    results = index.search("supply temperature high normal flow heat exchanger fouling", top_k=5)
    assert results
    assert results[0].doc_id == "SOP-TEMP-009"


def test_chunks_carry_source_metadata(index: RetrievalIndex) -> None:
    results = index.search("pump suction strainer blockage", top_k=3)
    assert results
    top = results[0]
    assert top.doc_id and top.title and top.section
    assert top.score > 0
    assert top.metadata.get("source", "").endswith(".md")


def test_search_before_index_raises() -> None:
    with pytest.raises(RuntimeError):
        RetrievalIndex().search("anything")


def test_manual_documents_can_be_indexed() -> None:
    index = RetrievalIndex()
    count = index.index(
        [
            Document(doc_id="D1", title="Doc One", section="A", text="alpha beta gamma"),
            Document(doc_id="D2", title="Doc Two", section="B", text="delta epsilon zeta"),
        ]
    )
    assert count == 2
    hits = index.search("alpha", top_k=1)
    assert hits and hits[0].doc_id == "D1"
