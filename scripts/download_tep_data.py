#!/usr/bin/env python
"""Download + convert the Tennessee Eastman Process dataset (Prompt 8).

Source: Rieth, C. A., et al. "A Dataset for the Tennessee Eastman Process
(TEP) Simulation", Harvard Dataverse, DOI: 10.7910/DVN/6C3JR1. The original
process benchmark is Downs & Vogel (1993), "A plant-wide industrial process
control problem for the chemical process industries", Computers & Chemical
Engineering 17(3). Cite BOTH when presenting.

The dataset is distributed as R workspaces (.RData). This script downloads
the needed files, reads them with `pyreadr` (no R installation required),
and writes plain CSVs under data/tep/ that the TEP replay engine streams.

Files / sizes (from the Dataverse API, ids pinned so re-runs are stable):
  3031241 TEP_FaultFree_Training.RData   24.7 MB   (always downloaded)
  3031242 TEP_Faulty_Training.RData     494.1 MB   (always downloaded;
                                        contains faultNumber 1..21, we
                                        extract Fault 4 = reactor cooling
                                        water inlet temperature step)
  3031240 TEP_FaultFree_Testing.RData    47.3 MB   (--with-testing only)
  3031243 TEP_Faulty_Testing.RData      836.9 MB   (--with-testing only)

DISK NOTE: `--with-testing` adds ~880 MB of raw .RData plus the converted
CSVs. The replay engine uses the TRAINING files (the fault is introduced
at sample 160 there), so testing data is optional; the flag exists for
completeness. Raw .RData files are deleted after conversion unless
--keep-raw is passed.

Usage (host venv; needs `pip install pyreadr pandas`):
    python scripts/download_tep_data.py [--with-testing] [--keep-raw]

Offline mirror path (Prompt 8): the full Dataverse download throttled to
~30 KB/s in one session (a multi-hour download for a ~500 MB file), so the
project also accepts the `small_tep` mirror (an 18.9 MB cropped redistribution
of the same Rieth et al. dataset -- all 52 variables, 210 runs, Fault 4
injected at sample 161) via:

    python scripts/download_tep_data.py --from-small-tep data/small_tep.zip

This converts the mirror's dataset.csv + labels.csv into the SAME
fault_free_training.csv / fault_4_training.csv the replay engine streams.
BOTH sources are cited in docs/real-data-sources.md; the mirror is only an
availability workaround, never a different dataset.

data/ is gitignored -- every developer/demo machine must run this script
once locally; the raw data is never committed.
"""
from __future__ import annotations

import argparse
import shutil
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data" / "tep"

DATAVERSE = "https://dataverse.harvard.edu/api/access/datafile/{file_id}"
FILES = {
    "TEP_FaultFree_Training.RData": 3031241,
    "TEP_Faulty_Training.RData": 3031242,
    "TEP_FaultFree_Testing.RData": 3031240,   # --with-testing
    "TEP_Faulty_Testing.RData": 3031243,      # --with-testing
}

FAULT_NUMBER = 4  # step change in reactor cooling water inlet temperature


def download(file_id: int, dest: Path) -> None:
    if dest.exists() and dest.stat().st_size > 1_000_000:
        print(f"  cached: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return
    url = DATAVERSE.format(file_id=file_id)
    print(f"  downloading {dest.name} ({url}) ...")
    tmp = dest.with_suffix(".part")
    # Dataverse 403s default urllib user agents -- send a browser-like one.
    request = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) ProcessGuardAI-prep/1.0",
        "Accept": "*/*",
    })
    with urllib.request.urlopen(request, timeout=120) as resp, open(tmp, "wb") as out:
        done = 0
        while True:
            chunk = resp.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            done += len(chunk)
            print(f"\r    {done / 1e6:8.1f} MB", end="", flush=True)
    print()
    tmp.rename(dest)


def to_csv(rdata_path: Path, csv_path: Path, fault_filter: int | None) -> int:
    """Read one .RData workspace, write CSV(s). Returns rows written."""
    import pyreadr  # host-only dependency (see module docstring)

    result = pyreadr.read_r(str(rdata_path))
    rows_written = 0
    for table_name, df in result.items():
        if fault_filter is not None:
            # The faulty workspaces carry a faultNumber column (1..21).
            fault_col = next(
                (c for c in df.columns if c.lower().replace(" ", "") in
                 ("faultnumber", "fault_number")), None
            )
            if fault_col is None:
                raise SystemExit(
                    f"{rdata_path.name}:{table_name} has no fault number column; "
                    f"columns: {list(df.columns)[:10]}"
                )
            df = df[df[fault_col] == fault_filter]
        # Simulation-run labels are irrelevant to the replay and would leak
        # into the stream payloads otherwise.
        df = df.drop(columns=[c for c in df.columns
                              if c.lower().replace(" ", "") == "simulationrun"])
        out = csv_path if len(result) == 1 else csv_path.with_name(
            f"{csv_path.stem}__{table_name}.csv")
        df.to_csv(out, index=False)
        rows_written += len(df)
        print(f"  wrote {out.name}: {len(df)} rows x {len(df.columns)} cols")
    return rows_written


def convert_small_tep(mirror: Path) -> int:
    """Convert the small_tep mirror (dataset.csv + labels.csv, inside `mirror`
    -- a .zip or an already-extracted directory) into the two training CSVs
    the replay engine expects. Pure stdlib; no pandas/pyreadr needed.

    Selection rules (documented in docs/real-data-sources.md):
      * fault_free_training.csv: all rows of the first run whose label is 0
        for every sample (a complete fault-free 960-sample training run).
      * fault_4_training.csv: the first run containing label == FAULT_NUMBER,
        trimmed to a window starting 100 samples before the fault onset
        (label != 0) -- the replay engine itself re-locates the onset from
        the retained `sample` column and starts FAULT_LEAD_IN before it.
    """
    import csv as _csv
    import zipfile

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    work = DATA_DIR / "small_tep_src"
    if mirror.suffix.lower() == ".zip":
        work.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(mirror) as zf:
            for name in ("dataset.csv", "labels.csv"):
                member = next(m for m in zf.namelist() if m.endswith(name))
                (work / name).write_bytes(zf.read(member))
        src = work
    else:
        src = mirror

    def read_rows(name: str) -> list[dict]:
        with open(src / name, newline="", encoding="utf-8") as fh:
            return list(_csv.DictReader(fh))

    dataset = read_rows("dataset.csv")
    labels = read_rows("labels.csv")
    label_of = {(r["run_id"], int(r["sample"])): int(r["labels"]) for r in labels}

    runs: dict[str, list[dict]] = {}
    for row in dataset:
        runs.setdefault(row["run_id"], []).append(row)
    for rows in runs.values():
        rows.sort(key=lambda r: int(r["sample"]))

    free_rows = next(
        (rows for rid, rows in sorted(runs.items())
         if all(label_of.get((rid, int(r["sample"])), 0) == 0 for r in rows)),
        None,
    )
    fault_rows = next(
        (rows for rid, rows in sorted(runs.items())
         if any(label_of.get((rid, int(r["sample"])), 0) == FAULT_NUMBER for r in rows)),
        None,
    )
    if free_rows is None or fault_rows is None:
        raise SystemExit(
            "small_tep mirror missing an all-zero run or a Fault-4 run; "
            f"saw {len(runs)} runs in {src / 'dataset.csv'}"
        )

    fault_run_id = fault_rows[0]["run_id"]
    onset = next(
        int(r["sample"]) for r in fault_rows
        if label_of.get((fault_run_id, int(r["sample"])), 0) == FAULT_NUMBER
    )
    lead = 100  # real in-band samples retained before the onset window
    window = [r for r in fault_rows if onset - lead <= int(r["sample"])]

    for name, rows in (
        ("fault_free_training.csv", free_rows),
        ("fault_4_training.csv", window),
    ):
        out = DATA_DIR / name
        with open(out, "w", newline="", encoding="utf-8") as fh:
            writer = _csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
        print(f"  wrote {out.name}: {len(rows)} rows")

    print(
        f"\nDone. Converted small_tep mirror -> {DATA_DIR}\\"
        "(fault_free_training.csv, fault_4_training.csv). "
        f"Fault-4 onset in the mirror at sample {onset} (Rieth et al.); "
        "cite BOTH the canonical DOI and the mirror -- see docs/real-data-sources.md."
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--with-testing", action="store_true",
                        help="also download the (large) testing workspaces")
    parser.add_argument("--keep-raw", action="store_true",
                        help="keep the .RData files after conversion")
    parser.add_argument("--from-small-tep", metavar="PATH",
                        help="offline path: convert the small_tep mirror (.zip "
                             "or extracted dir with dataset.csv/labels.csv) "
                             "instead of downloading from Dataverse")
    args = parser.parse_args()

    if args.from_small_tep:
        return convert_small_tep(Path(args.from_small_tep))

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    plan = {
        "TEP_FaultFree_Training.RData":
            ("fault_free_training.csv", None),
        "TEP_Faulty_Training.RData":
            ("fault_4_training.csv", FAULT_NUMBER),
    }
    if args.with_testing:
        plan["TEP_FaultFree_Testing.RData"] = ("fault_free_testing.csv", None)
        plan["TEP_Faulty_Testing.RData"] = ("fault_4_testing.csv", FAULT_NUMBER)

    for rdata_name, (csv_name, fault) in plan.items():
        rdata_path = DATA_DIR / rdata_name
        csv_path = DATA_DIR / csv_name
        if csv_path.exists():
            print(f"  exists: {csv_name}")
            continue
        download(FILES[rdata_name], rdata_path)
        to_csv(rdata_path, csv_path, fault)
        if not args.keep_raw:
            rdata_path.unlink()  # reclaim the disk (the CSV is the artifact)

    print("\nDone. CSVs in", DATA_DIR)
    print("data/ is gitignored -- re-run this script on any new machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
