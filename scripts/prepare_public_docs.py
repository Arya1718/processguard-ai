#!/usr/bin/env python
"""Download + convert REAL public documents into the knowledge base (Prompt 8).

Two U.S. Government works (public domain), cited with working URLs:

  1. U.S. Department of Energy, Federal Energy Management Program (FEMP),
     "Best Management Practice #10: Cooling Tower Management"
     https://www.energy.gov/femp/best-management-practice-10-cooling-tower-management
     -> app/rag/knowledge_base/PUB-DOE-FEMP-COOLING-TOWER.md

  2. U.S. Department of Energy, "Improving Pumping System Performance:
     A Sourcebook for Industry" (2nd ed., DOE/GO-102008-2331, with the
     Alliance to Save Energy / Hydraulic Institute / NASA)
     https://www.energy.gov/sites/prod/files/2014/05/f16/pump.pdf
     -> app/rag/knowledge_base/PUB-DOE-PUMP-SOURCEBOOK.md
     (the two operationally relevant sections are extracted from the
     122-page sourcebook: "Common Pumping System Problems" and "Basic Pump
     Maintenance" incl. its checklist; a full dump would bury retrieval
     under running-header boilerplate)

Every generated file carries a front-matter comment with
source_type: public_real -- corpus.py parses it and the RetrievalIndex
metadata carries it through to every citation. The authored SOPs
(SOP-*.md) are illustrative and say so in their own front matter.

Usage:
    python scripts/prepare_public_docs.py            # download + convert
    python scripts/prepare_public_docs.py --check    # verify KB files exist
                                                     # and carry provenance

Network note for CI: --check is offline and CI-safe; the download step
needs internet access and is a documented CI tradeoff (see
docs/real-data-sources.md) rather than a silent skip.
"""
from __future__ import annotations

import argparse
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DIR = REPO_ROOT / "agent-service" / "app" / "rag" / "knowledge_base"
DOC_CACHE = REPO_ROOT / "data" / "public_docs"

USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) ProcessGuardAI-prep/1.0"

FEMP_URL = (
    "https://www.energy.gov/femp/"
    "best-management-practice-10-cooling-tower-management"
)
FEMP_TITLE = "PUB-DOE-FEMP-COOLING-TOWER: DOE FEMP Best Management Practice #10 - Cooling Tower Management"

PUMP_PDF_URL = "https://www.energy.gov/sites/prod/files/2014/05/f16/pump.pdf"
PUMP_TITLE = (
    "PUB-DOE-PUMP-SOURCEBOOK: DOE Improving Pumping System Performance - "
    "A Sourcebook for Industry (2nd ed.)"
)

# PDF page indexes (0-based) of the two operationally relevant sections,
# verified against the downloaded sourcebook (122 pages). Selected ranges
# instead of all 122 pages: a full dump would bury retrieval under the
# running-header boilerplate ("A Sourcebook for Industry" on every page).
PUMP_EXTRACTS = (
    ("PUMP-PROBLEMS: Common Pumping System Problems (incl. cavitation, pump wear, seals, and indications of oversized pumps)",
     22, 31),
    ("PUMP-MAINTENANCE: Basic Pump Maintenance (incl. the Basic Maintenance Checklist and predictive maintenance)",
     36, 40),
)

_RUNNING_HEADERS = {
    "A Sourcebook for Industry",
    "Improving Pumping System Performance",
    "Section 2: Performance Improvement Opportunity Roadmap",
}


def _clean_sourcebook_page(text: str) -> str:
    lines: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line in _RUNNING_HEADERS:
            continue
        if re.fullmatch(r"\d+", line):  # bare printed page number
            continue
        # "N | <running header>" lines (pypdf merges header + number)
        if re.fullmatch(r"\d+ \|[^\n]*", line) and any(
            h in line for h in _RUNNING_HEADERS
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


def front_matter(source_type: str, source_url: str, publisher: str) -> str:
    """HTML-comment front matter parsed by app/rag/corpus.py."""
    return (
        "<!--\n"
        f"source_type: {source_type}\n"
        f"source_url: {source_url}\n"
        f"source_publisher: {publisher}\n"
        f"retrieved: 2026-09-20\n"
        "license_note: U.S. federal government work - public domain\n"
        "-->\n"
    )


def fetch(url: str, dest: Path) -> Path:
    if dest.exists() and dest.stat().st_size > 10_000:
        print(f"  cached: {dest.name} ({dest.stat().st_size / 1e6:.1f} MB)")
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  downloading {url}")
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=120) as resp:
        dest.write_bytes(resp.read())
    print(f"  saved {dest.name} ({dest.stat().st_size / 1e6:.2f} MB)")
    return dest


def html_to_text(raw: str) -> str:
    """Main-content extraction for the FEMP page: take the <main> element,
    cut from the document intro to the footer's Related Links block, strip
    tags, drop nav boilerplate lines."""
    main = re.search(r"(?is)<main\b.*?</main>", raw)
    body = main.group(0) if main else raw
    start = body.find("Cooling towers dissipate")  # the BMP's intro sentence
    if start >= 0:
        body = body[start:]
    end = body.find("Related Links")
    if end >= 0:
        body = body[:end]
    body = re.sub(r"(?s)<[^>]+>", " ", body)
    body = re.sub(r"&nbsp;|&#160;|&amp;", " ", body)
    body = re.sub(r"[ \t]+", " ", body)
    lines = [
        ln.strip() for ln in body.splitlines()
        if ln.strip()
        and ln.strip() not in {"Skip to main content", "Lock", "Locked padlock"}
        and not ln.startswith("An official website")
        and not ln.startswith("Secure .gov websites")
        and not ln.startswith("A .gov website")
        and not ln.startswith("A lock")
        and not ln.startswith(") or https://")
    ]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def build_femp_doc() -> None:
    dest = KB_DIR / "PUB-DOE-FEMP-COOLING-TOWER.md"
    html = fetch(FEMP_URL, DOC_CACHE / "femp_bmp10_cooling_tower.html")
    text = html_to_text(html.read_text(encoding="utf-8", errors="replace"))
    intro = (
        front_matter(
            "public_real", FEMP_URL,
            "U.S. Department of Energy, Federal Energy Management Program",
        )
        + "# " + FEMP_TITLE + "\n\n"
        "> **Provenance: public_real.** Verbatim public-domain guidance from\n"
        "> the U.S. Department of Energy, Federal Energy Management Program\n"
        f"> (FEMP). Source: {FEMP_URL}\n"
        "> Retrieved 2026-09-20. Lightly formatted (headings/bullets) for the\n"
        "> retrieval index; wording preserved. Buckman/Ackumen-specific numbers\n"
        "> are NOT in this document -- it is federal facility guidance.\n\n"
    )
    dest.write_text(intro + text + "\n", encoding="utf-8")
    print(f"  wrote {dest.relative_to(REPO_ROOT)} ({len(text)} chars of content)")


PUMP_SECTIONS = {
    "Common Pumping System Problems": "PUMP-PROBLEMS",
    "Basic Pump Maintenance": "PUMP-MAINTENANCE",
}


def build_pump_doc() -> None:
    """Extract the two operationally relevant sections from the sourcebook."""
    from pypdf import PdfReader  # host-only dependency (pip install pypdf)

    dest = KB_DIR / "PUB-DOE-PUMP-SOURCEBOOK.md"
    pdf_path = fetch(PUMP_PDF_URL, DOC_CACHE / "doe_pump_sourcebook.pdf")
    reader = PdfReader(str(pdf_path))
    out = [
        front_matter(
            "public_real", PUMP_PDF_URL,
            "U.S. Department of Energy (Industrial Technologies Program), with the "
            "Alliance to Save Energy, Hydraulic Institute, and NASA",
        )
        + "# " + PUMP_TITLE + "\n\n"
        "> **Provenance: public_real.** Excerpted verbatim from the U.S.\n"
        "> Department of Energy industrial sourcebook (public domain), with\n"
        "> the Alliance to Save Energy, Hydraulic Institute, and NASA.\n"
        f"> Full document: {PUMP_PDF_URL}\n"
        "> Retrieved 2026-09-20. Only the two operationally relevant sections\n"
        "> are indexed here (the full 122 pages would bury retrieval under\n"
        "> running-header boilerplate); wording preserved, running headers and\n"
        "> page numbers removed.\n\n"
    ]
    for heading, first, last in PUMP_EXTRACTS:
        body = _clean_sourcebook_page(
            "\n".join(page.extract_text() or "" for page in reader.pages[first:last])
        )
        if len(body) < 1000:
            raise SystemExit(
                f"section {heading!r} extracted too little text ({len(body)}); "
                "the sourcebook page layout may have changed -- verify "
                "PUMP_EXTRACTS ranges"
            )
        out.append(f"## {heading}\n\n{body}\n")
        print(f"  {heading.split(':')[0]}: {len(body)} chars")

    dest.write_text("\n".join(out), encoding="utf-8")
    print(f"  wrote {dest.relative_to(REPO_ROOT)}")


def check() -> int:
    failures = 0
    for name in ("PUB-DOE-FEMP-COOLING-TOWER.md", "PUB-DOE-PUMP-SOURCEBOOK.md"):
        path = KB_DIR / name
        if not path.exists():
            print(f"MISSING: {path.relative_to(REPO_ROOT)} (run this script)")
            failures += 1
            continue
        head = path.read_text(encoding="utf-8")[:400]
        if "source_type: public_real" not in head:
            print(f"BAD PROVENANCE: {name} lacks source_type: public_real")
            failures += 1
        else:
            print(f"OK: {name} (public_real provenance present)")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--check", action="store_true",
                        help="verify KB files exist and carry provenance (offline)")
    args = parser.parse_args()
    if args.check:
        return check()
    DOC_CACHE.mkdir(parents=True, exist_ok=True)
    build_femp_doc()
    build_pump_doc()
    print("\nDone. Restart the agent service to re-index (load_corpus runs at startup).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
