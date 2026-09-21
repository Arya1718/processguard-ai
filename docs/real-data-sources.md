# Real Data Sources (Prompt 8)

Honest accounting of what in ProcessGuard AI is **real public data** vs.
**authored for this project** (`source_type: illustrative`), and the CI
tradeoff that keeps the distinction verifiable rather than implied.

Every document, sensor stream, and historical incident in the system carries
this label in-band — in file front matter, in DB `SourceType` columns, and
as a visible provenance badge in the UI. A missing label **defaults to
`illustrative`**, never to `public_real`.

## Summary

| Artifact | source_type | Real source | Where it lives |
|---|---|---|---|
| TEP process samples (temperature, flow) | `public_real` | Rieth et al. 2016, Harvard Dataverse | `data/tep/*.csv`, streamed by `app/simulator/tep_replay.py` |
| pH / conductivity / vibration sensors | `illustrative` | Synthetic, correlated overlay | generated in-memory by `tep_replay.py::_Overlay` |
| Cooling-tower management guidance | `public_real` | U.S. DOE FEMP BMP #10 | `app/rag/knowledge_base/PUB-DOE-FEMP-COOLING-TOWER.md` |
| Pump industry sourcebook | `public_real` | U.S. DOE sourcebook (with AEE/HI/NASA) | `app/rag/knowledge_base/PUB-DOE-PUMP-SOURCEBOOK.md` |
| Authored SOPs (SOP-*) | `illustrative` | Written for this case study | `app/rag/knowledge_base/SOP-*.md` |
| 17-day-ago CP-04 incident (HIST-2026-0141) | `illustrative` | Authored scenario | `HistoricalIncidents` table, seeded by `DemoSeeder.cs` |
| CSB MFG Chemical incident | `public_real` | U.S. Chemical Safety Board report | `HistoricalIncidents` table, seeded by `DemoSeeder.cs` |

---

## 1. Tennessee Eastman Process (TEP) — real process data

**Canonical source:**
Rieth, C. A., Arnold, A. C., Atia, H., & others. "A Dataset for the
Tennessee Eastman Process (TEP) Simulation." Harvard Dataverse, DOI:
[10.7910/DVN/6C3JR1](https://doi.org/10.7910/DVN/6C3JR1), 2016.

**Original benchmark:**
Downs, D. A., & Vogel, E. F. "A plant-wide industrial process control
problem for the chemical process industries." *Computers & Chemical
Engineering* 17(3): 225–235, 1993.

Both are cited wherever TEP is referenced (in source headers, on data
loading, and here). The Harvard Dataverse DOI is the primary citation;
Downs & Vogel is cited as the original benchmark.

### What is streamed as real

The replay engine (`app/simulator/tep_replay.py`) reads the converted
CSV files and maps two TEP variables into the demo sensor bands:

| Demo sensor | TEP variable | Meaning |
|---|---|---|
| `temperature` | XMEAS(9) | Reactor temperature (degC) |
| `flow_rate` | XMV(10) | Reactor cooling-water flow valve (% open) |

These values are **real** TEP measurements, affinely mapped into the demo
sensor normal bands:

```
mapped = band_centre + (raw - src_mean) / src_sigma * band_width * 0.10
```

The mapping parameters (measured once on the converted training runs,
never re-fit per run):

- XMEAS(9): mean 120.400 degC, sigma 0.019 (controller-tight band)
- XMV(10): mean 41.12 %, sigma 0.539 (valve hunting band)
- `BAND_PER_SIGMA = 0.10` (1 real sigma = 10% of sensor band width)

This gain is chosen so that healthy fault-free data **never** trips the
static band check (max free-run `|z|` = 3.73 → mapped overshoot < 5% of
band), while the real Fault-4 step is far outside it.

### The XMV(10) finding — what the demo shows

**Fault 4 = step change in the reactor cooling-water INLET temperature.**

The documented, real TEP signature for Fault 4:

1. The reactor temperature controller compensates the disturbance by
   opening the cooling-water valve (XMV(10)).
2. The valve **steps from ~41% to ~45% and holds** — sustained z ≥ 3.82,
   mean 7.0, onset spike z = 11.65 (measurable from sample ~181 in the
   Rieth training set).
3. XMEAS(9) (reactor temperature) **stays AT setpoint** through the fault
   — the controller saturates.

This is the **real-world "cooling capacity fading" symptom**: temperature
stays normal, but the flow needed to keep it normal has jumped and stays
jumped. That is exactly what the Detection Agent is built to catch.

The demo's mapped flow rate shows the real step (130–141 m³/h vs the
118–132 band), and the mapped temperature stays in band — reproducing
the real signal faithfully, including timing and shape.

### The CSB report as a historical analog

The public_real `public_real` historical incident is:

> U.S. Chemical Safety Board, Investigation Report 2004-9-I-GA, "MFG
> Chemical Inc., Boiking/Blowdown Incident and Toxic Gas Release,"
> Dalton, Georgia, April 12, 2004.
>
> Source: [https://www.csb.gov/mfg-chemical-inc-toxic-gas-release/](https://www.csb.gov/mfg-chemical-inc-toxic-gas-release/)

Seeded in `middleware/src/Data/DemoSeeder.cs` (lines 175–185) with
`source = "csb-gov:2004-9-I-GA-MFG"`, `sourceType = "public_real"`, and
the working citation URL above. It is used as an **equipment analog** —
symptom keywords (temperature/cooling) let `find_similar_history` match
it against heat-rejection anomalies, surfacing a real published
investigation's root cause and CSB-recommended lessons when the demo's
temperature channel is flagged.

### Known limitation: pH, conductivity, vibration

**The TEP dataset does NOT contain pH, conductivity, or vibration
measurements.** Those three sensors exist in the demo site because water
treatment plants monitor them, but TEP is a petrochemical pilot plant
with no such channels.

These values are a **synthetic overlay** (`_Overlay` class in
`tep_replay.py`) that is:

- CORRELATED with the real fault axis — ramps only while the real
  Fault-4 window is active (the overlay's `active` flag comes straight
  from the replay cursor, so all five sensors drift together as they
  would if a real cooling fault propagated into water chemistry).
- **Plainly labeled illustrative** — `provenance` field in every emitted
  event reads `"public_real (TEP CSV values) + illustrative overlay for
  ph/conductivity/vibration"` and the UI badge shows `illustrative` for
  those three citation points.

This is not hidden: `test_free_replay_drift_noise_is_rare_and_documented`
in `tests/test_tep_replay_detection.py` documents that XMV(10) valve
hunting occasionally produces isolated drift flags (~1% of readings,
scale-invariant), and the overlay is excluded from that assertion
entirely because it is not real data.

### The small_tep mirror

The full Dataverse download (~500 MB for the faulty training workspace)
is throttled to ~30 KB/s in some sessions — a multi-hour download. The
project also accepts an **offline mirror** (`small_tep.zip`, 18.9 MB)
containing the same Rieth et al. dataset (all 52 variables, 210 runs,
Fault 4 injected at sample 161):

```
python scripts/download_tep_data.py --from-small-tep data/small_tep.zip
```

This produces the SAME `fault_free_training.csv` / `fault_4_training.csv`
the replay engine streams. **Both the canonical DOI and the mirror are
cited here** — the mirror is only an availability workaround, never a
different dataset. The mirror is not committed (`data/` is gitignored).

### CI tradeoff: no guaranteed network access

`data/` is gitignored — every developer and demo machine must run
`scripts/download_tep_data.py` once locally. CI has no guaranteed
network access, so:

- Tests that need the **full** converted runs (`test_fault_window_starts_lead_in_before_real_onset`,
  `test_free_replay_*`) **skip with an explicit message** when
  `data/tep/*.csv` is absent — a documented, non-silent skip:
  `"converted TEP runs not present -- run scripts/download_tep_data.py
  --from-small-tep data/small_tep.zip on a machine with the mirror"`
- The **embedded excerpt** (`_EMBEDDED_EXCERPT` in `tep_replay.py`: 12
  real TEP samples, 6 fault-free and 6 from Fault 4's saturated window)
  keeps the core fault test (`test_fault4_embedded_excerpt_still_flags_incident`)
  runnable in CI with no network.
- `scripts/prepare_public_docs.py --check` is offline and CI-safe — it
  verifies the committed KB files exist and carry `source_type: public_real`
  in their front matter, without downloading anything.

To regenerate the KB documents (requires network), run
`scripts/prepare_public_docs.py` (without `--check`) on a machine with
internet access, then restart the agent service to re-index.

---

## 2. Public knowledge-base documents

Two real, publicly published U.S. government works (public domain) are
downloaded, lightly formatted, and committed to
`agent-service/app/rag/knowledge_base/`:

### PUB-DOE-FEMP-COOLING-TOWER.md

> U.S. Department of Energy, Federal Energy Management Program (FEMP).
> "Best Management Practice #10: Cooling Tower Management."
>
> URL: [https://www.energy.gov/femp/best-management-practice-10-cooling-tower-management](https://www.energy.gov/femp/best-management-practice-10-cooling-tower-management)
>
> Retrieved 2026-09-20. License: U.S. federal government work — public domain.

Generated by `scripts/prepare_public_docs.py::build_femp_doc()`. The full
HTML page is converted to text; the content is verbatim (only
headings/bullets reformatted, running headers stripped).

### PUB-DOE-PUMP-SOURCEBOOK.md

> U.S. Department of Energy, Industrial Technologies Program, with the
> Alliance to Save Energy, Hydraulic Institute, and NASA. "Improving Pumping
> System Performance: A Sourcebook for Industry" (2nd ed., DOE/GO-102008-2331).
>
> URL: [https://www.energy.gov/sites/prod/files/2014/05/f16/pump.pdf](https://www.energy.gov/sites/prod/files/2014/05/f16/pump.pdf)
>
> Retrieved 2026-09-20. License: U.S. federal government work — public domain.

Only the two operationally relevant sections are extracted (page ranges
22–31 "Common Pumping System Problems" and 36–40 "Basic Pump
Maintenance" including its checklist). A full 122-page dump would bury
retrieval under running-header boilerplate. Generated by
`scripts/prepare_public_docs.py::build_pump_doc()`.

### Provenance enforcement

Both files carry front matter parsed by `app/rag/corpus.py`:

```markdown
<!--
source_type: public_real
source_url: https://...
source_publisher: ...
retrieved: 2026-09-20
license_note: U.S. federal government work - public domain
-->
```

`corpus.py::_parse_front_matter` extracts `source_type` and `source_url`
into each Document's metadata; `RetrievalIndex` carries it through to
every citation. The UI (`EvidenceCitation.jsx`) renders a **visible
badge** (`public` for `public_real`, `illustrative` otherwise) and emits
a working link only for `public_real` sources.

`test_public_real_documents_carry_working_citation_urls` enforces that
every public_real chunk has an `https://` `source_url` and that no
citation URL contains an internal hostname.

---

## 3. Authored knowledge (illustrative)

The four SOP files (`SOP-COOL-014.md`, `SOP-CHEM-021.md`,
`SOP-DOSE-007.md`, `SOP-TEMP-009.md`) are **authored for this project**,
modeled on real industrial operational patterns but **not sourced from
Buckman internally** (Buckman/Ackumen operational data is proprietary and
was never used). Each declares `source_type: illustrative` in its front
matter; the honest default in `corpus.py` is `illustrative` when no front
matter is present.

The demo-seed historical incidents (`demo-seed:HIST-2026-0141`,
`demo-seed:HIST-2026-0097`, `demo-seed:HIST-2026-0042`,
`demo-seed:HIST-2026-0113`) are the same — authored scenario rows, seeded
with `SourceType = 'illustrative'` by default in `DemoSeeder.cs`.

---

## 4. CI tradeoff summary

| Need | Runs in CI? | What happens if it doesn't |
|---|---|---|
| Full TEP replay (`data/tep/*.csv`) | No (gitignored, network download) | Embedded 12-sample excerpt keeps the fault test running; full-run tests skip with explicit message |
| KB documents (`*.md` in `knowledge_base/`) | Yes — committed | `--check` passes offline; `--download` requires network |
| CSB historical incident | Yes — committed seed | `DemoSeeder.cs` seeds it at startup from the embedded record |
| Network downloads (`download_tep_data.py`, `prepare_public_docs.py` without `--check`) | No | Must run on a machine with internet; documented here |

The tradeoff is: **real-data fidelity vs. offline CI reproducibility.**
The system never silently falls back — absent real data it uses the
embedded real-TEM excerpt (still real TEP values, just 12 samples) and
logs a warning, or skips tests with an explicit reason pointing at this
document.
