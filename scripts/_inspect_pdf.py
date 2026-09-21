"""Quick PDF structure inspection (dev helper, not committed logic)."""
from pypdf import PdfReader

r = PdfReader("data/public_docs/doe_pump_sourcebook.pdf")
for i in (21, 27, 28, 33, 34, 38, 39):
    text = r.pages[i].extract_text() or ""
    print(f"===== page idx {i} =====")
    print(text[:900])
    print()
