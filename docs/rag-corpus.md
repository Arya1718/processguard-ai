# RAG Corpus & Retrieval

What the Knowledge Agent retrieves from, how it is indexed, and how to grow it.

## What's in the knowledge base

`agent-service/app/rag/knowledge_base/*.md` — plain markdown SOPs, one file
per procedure. Seeded corpus:

| File | Covers | Why it exists |
|---|---|---|
| `SOP-COOL-014.md` | Cooling Water Flow and Vibration Response | THE reference-scenario SOP: flow drop >15% + vibration above threshold → inspect pump suction, check filter blockage, verify pump condition, reduce load if temperature keeps rising |
| `SOP-DOSE-007.md` | Chemical Dosing Skid Calibration | Unrelated procedure so retrieval must discriminate between documents |
| `SOP-CHEM-021.md` | Cooling Tower pH and Conductivity Excursion | Overlapping vocabulary (pH, conductivity) but a *chemistry* problem — retrieval must not confuse it with the pump-mechanics signature |
| `SOP-TEMP-009.md` | Heat Exchanger Temperature Excursion | Temperature-only symptom pattern; points to SOP-COOL-014 when flow/vibration also leave band |

Each file is split into one indexed document per `##` section. The section
text is indexed together with the document title and heading, so queries
matching an SOP id or name still rank it.

## How indexing works

`app/rag/retrieval.py::RetrievalIndex` — TF-IDF (scikit-learn,
1–2 word n-grams, sublinear tf, English stop words) + cosine similarity.

**AZURE AI SEARCH NOTE:** this is a local stand-in for Azure AI Search's
vector index. It is classic retrieval math — no embeddings API, no LLM —
so it stays free and dependency-light for local dev. A real embedding-based
index replaces it behind the same interface (`index(documents)` /
`search(query, top_k)`) with no change to any agent code.

The index is built once at agent-service startup (`app/main.py` loads the
corpus via `app/rag/corpus.py::load_corpus`).

## How the Knowledge Agent queries it

For an incident whose anomaly summary flags (for example) flow_rate,
vibration, temperature, ph, the query is built from the sensor types plus
the per-sensor deviation reasons — so the combined mechanical signature
("flow_rate ... outside normal band ... vibration ...") ranks SOP-COOL-014
above the chemistry-only and temperature-only SOPs. Top 4 chunks are kept,
with `docId`, `title`, `section`, `score`, and a short excerpt as the
citation metadata.

Historical incidents are **not** retrieved through TF-IDF; they come from
the `HistoricalIncidents` table via `find_similar_history` (same equipment
+ overlapping symptom keywords — deliberately not a second ML model).

## Adding more documents later

1. Drop a new `SOP-XXX-NNN.md` into `app/rag/knowledge_base/` with the same
   structure (`# Title`, `## Section` headings).
2. Restart the agent service — the corpus is re-indexed at startup.
3. Nothing else: no registration list, no code change. New documents are
   picked up automatically by `load_corpus()`.

When swapping in Azure AI Search later: implement the same
`index`/`search` interface against the real service, move the corpus upload
to a setup pipeline, and keep the citation metadata (`docId`, `title`,
`section`) identical so the Root-Cause prompt and provenance docs do not
change.
