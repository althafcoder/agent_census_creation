"""Rebuild the Vikta medical census workbook from its extracted text into tests/fixtures (test data only)."""
from datetime import datetime
from pathlib import Path

from openpyxl import Workbook

root = Path(__file__).resolve().parents[1]
src = root / "extracted_text" / "Vikta_Energy_Technologies_-_Medical_Census_extracted.txt"
lines = src.read_text(encoding="utf-8").splitlines()
start = next(i for i, l in enumerate(lines) if l.startswith("=== Sheet: Census"))
rows = []
for l in lines[start + 1:]:
    if " | " not in l:
        if rows:
            break
        continue
    rows.append([c.strip() for c in l.split(" | ")])
hdr, data = rows[0], rows[1:]
w = len(hdr)
wb = Workbook()
ws = wb.active
ws.title = "Census"
ws.append(hdr)
for r in data:
    r = (r + [""] * w)[:w]
    out_row = []
    for i, v in enumerate(r):
        if v and (hdr[i].startswith("*Employee") or hdr[i] == "Annual Pay"):
            out_row.append(int(float(v)))
        elif v and "Birth" in hdr[i]:
            out_row.append(datetime.strptime(v[:10], "%Y-%m-%d"))
        else:
            out_row.append(v or None)
    ws.append(out_row)
out = root / "tests" / "fixtures" / "coded_relationship_census.xlsx"
out.parent.mkdir(parents=True, exist_ok=True)
wb.save(out)
print(out, len(data), sum(1 for r in data if r[0] == "1"), sum(1 for r in data if r[0] == "0"))
