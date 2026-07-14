#!/usr/bin/env python3
"""Quick week-over-week read-rate change by geography.
read% = read / (read + delivered)  [of messages that reached a device].
Uses each week's most-matured pull. Summary tab + per-level detail tabs.
NOTE: wk1 pull is ~5 days post-send vs ~6 for wk2/wk3, so wk1 read% is
slightly understated (flagged in the file)."""
import csv, re
from collections import defaultdict
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, Alignment

import config
DATA = config.DATA_DIR
DISSEM = config.require_dissem()
LABELS = config.REFERENCE_DIR / "geography_labels.xlsx"
OUT = DATA / "read_rate_WoW_change.xlsx"

# >>> EDIT PER RUN <<< one (label, pulled-CSV) pair per week to compare. Use each
# week's most-matured pull, and compare at MATCHED maturity (equal days-after-send)
# for the numbers to be valid.
WEEKS = [
    ("wk1", DATA / "REPLACE_with_week1_pull.csv"),
    ("wk2", DATA / "REPLACE_with_week2_pull.csv"),
    ("wk3", DATA / "REPLACE_with_week3_pull.csv"),
]
NW = len(WEEKS)

def collapse(s): return re.sub(r"\s+", " ", s.strip())
def _k(s): return collapse(s).lower()
dd, dm, dc = {}, {}, {}
try:
    ob = openpyxl.load_workbook(LABELS, read_only=True)
    for r in ob["DF By Mandal"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL" and r[1]:
            dd[_k(r[0])] = collapse(r[0]); dm[_k(r[1])] = collapse(r[1])
    for r in ob["DF By Cluster"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL" and r[2]:
            dc[_k(r[2])] = collapse(r[2])
    ob.close()
except FileNotFoundError:
    pass  # no reference labels -> fall back to .title()
def D(s): return dd.get(_k(s), collapse(s).title())
def M(s): return dm.get(_k(s), collapse(s).title())
def C(s): return dc.get(_k(s), collapse(s).title())

print("loading geography...")
geo = {}
with open(DISSEM, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        geo[row["ppbno"].strip()] = (D(row["district_name"]), M(row["mandal_name"]), C(row["cluster_name"]))

# per-level: key -> per-week [read, delivered]
def mk(): return [[0, 0] for _ in range(NW)]
lv_d = defaultdict(mk); lv_m = defaultdict(mk); lv_c = defaultdict(mk)
for wi, (lbl, path) in enumerate(WEEKS):
    print(f"aggregating {lbl} ...")
    with open(path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            st = row["last_status"]
            if st not in ("read", "delivered"):
                continue
            g = geo.get(row["ppbno"].strip())
            if not g:
                continue
            j = 0 if st == "read" else 1
            lv_d[(g[0],)][wi][j] += 1
            lv_m[(g[0], g[1])][wi][j] += 1
            lv_c[g][wi][j] += 1

def pct(rd):  # read% of reached
    r, d = rd
    return (r / (r + d)) if (r + d) else None

wb = openpyxl.Workbook(); wb.remove(wb.active)
PCT = "0.0%"; PP = "+0.0;-0.0"
hf = Font(bold=True)

def detail_tab(name, labels, store):
    ws = wb.create_sheet(name)
    hdr = labels + [f"Read% {w[0]}" for w in WEEKS] + ["Δ wk1→wk2 (pp)", "Δ wk2→wk3 (pp)"]
    ws.append(hdr)
    nl = len(labels)
    sums = [0.0] * NW; cnts = [0] * NW; d12 = []; d23 = []
    for key in sorted(store):
        ps = [pct(store[key][wi]) for wi in range(NW)]
        row = list(key) + [p if p is not None else None for p in ps]
        c12 = (ps[1] - ps[0]) * 100 if (ps[0] is not None and ps[1] is not None) else None
        c23 = (ps[2] - ps[1]) * 100 if (ps[1] is not None and ps[2] is not None) else None
        row += [c12, c23]
        ws.append(row)
        for wi in range(NW):
            if ps[wi] is not None: sums[wi] += ps[wi]; cnts[wi] += 1
        if c12 is not None: d12.append(c12)
        if c23 is not None: d23.append(c23)
    # average row (unweighted across units)
    avg = ["AVERAGE"] + [None] * (nl - 1)
    avg += [(sums[wi] / cnts[wi]) if cnts[wi] else None for wi in range(NW)]
    avg += [sum(d12) / len(d12) if d12 else None, sum(d23) / len(d23) if d23 else None]
    ws.append(avg)
    # formats
    for r in range(2, ws.max_row + 1):
        for c in range(nl + 1, nl + 1 + NW): ws.cell(r, c).number_format = PCT
        for c in range(nl + 1 + NW, nl + 3 + NW): ws.cell(r, c).number_format = PP
    for c in range(1, len(hdr) + 1):
        ws.cell(1, c).font = hf
        ws.cell(ws.max_row, c).font = hf
    ws.freeze_panes = "A2"
    return [(sums[wi] / cnts[wi]) if cnts[wi] else None for wi in range(NW)], \
           (sum(d12) / len(d12) if d12 else None), (sum(d23) / len(d23) if d23 else None), store

# build detail tabs and collect summary
summary = {}
summary["District"] = detail_tab("By District", ["District"], lv_d)
summary["Mandal"]   = detail_tab("By Mandal", ["District", "Mandal"], lv_m)
summary["Cluster"]  = detail_tab("By Cluster", ["District", "Mandal", "Cluster"], lv_c)

# summary tab (front)
sw = wb.create_sheet("Summary", 0)
sw.append(["Average read% (of reached) and week-over-week change"])
sw.append(["NOTE: wk1 pull ~5 days post-send vs ~6 for wk2/wk3, so wk1 read% is slightly understated."])
sw.append([])
sw.append(["Level", f"Avg Read% {WEEKS[0][0]}", f"Avg Read% {WEEKS[1][0]}", f"Avg Read% {WEEKS[2][0]}",
           "Avg Δ wk1→wk2 (pp)", "Avg Δ wk2→wk3 (pp)", "(units)"])
nunits = {"District": len(lv_d), "Mandal": len(lv_m), "Cluster": len(lv_c)}
for lvl in ("District", "Mandal", "Cluster"):
    avgs, a12, a23, store = summary[lvl]
    sw.append([lvl] + avgs + [a12, a23, nunits[lvl]])
# pooled overall row (volume-weighted, from district totals)
pool = [[0, 0] for _ in range(NW)]
for key in lv_d:
    for wi in range(NW):
        pool[wi][0] += lv_d[key][wi][0]; pool[wi][1] += lv_d[key][wi][1]
pp = [pct(pool[wi]) for wi in range(NW)]
sw.append([])
sw.append(["POOLED (all farmers, volume-weighted)"] + pp +
          [(pp[1]-pp[0])*100, (pp[2]-pp[1])*100, ""])
# formats
for r in range(5, sw.max_row + 1):
    for c in (2, 3, 4): sw.cell(r, c).number_format = PCT
    for c in (5, 6): sw.cell(r, c).number_format = PP
for c in range(1, 8): sw.cell(4, c).font = hf
sw.cell(1, 1).font = Font(bold=True, size=13)
sw.column_dimensions["A"].width = 36
for col in "BCDEFG": sw.column_dimensions[col].width = 18

wb.save(OUT)
print(f"\nSaved -> {OUT}")
for lvl in ("District", "Mandal", "Cluster"):
    avgs, a12, a23, _ = summary[lvl]
    f = lambda x: f"{x*100:.1f}%" if x is not None else "NA"
    g = lambda x: f"{x:+.2f}pp" if x is not None else "NA"
    print(f"  {lvl:9}: read% {f(avgs[0])} -> {f(avgs[1])} -> {f(avgs[2])} | avg WoW {g(a12)}, {g(a23)}")
