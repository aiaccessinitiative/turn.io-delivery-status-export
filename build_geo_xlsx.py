#!/usr/bin/env python3
"""Build the geographic delivery report -> delivery_status_by_geography.xlsx.

Joins the most recent anon pull (ppbno -> last_status) to the dissemination
roster (ppbno -> district/mandal/cluster) and writes an 8-tab workbook:
  * "DeliveredFailed" view: Total Sent / Delivered / Failed / %s
  * "Full Breakdown" view:  Sent / Delivered / Read / Failed / Total / %s
each by district, mandal, and cluster. no_status (tiny) folds into 'sent' so
totals tie to the pulled row count.

Geography display labels use an OPTIONAL reference workbook
(REFERENCE_DIR/geography_labels.xlsx, same 8-tab layout) for canonical Title-Case
names; without it, labels fall back to Python .title().
"""
import csv, glob, os
from collections import defaultdict
from pathlib import Path
import openpyxl
from openpyxl.styles import Font, Alignment

import config
DATA   = config.DATA_DIR
DISSEM = config.require_dissem()
LABELS = config.REFERENCE_DIR / "geography_labels.xlsx"
OUT    = DATA / "delivery_status_by_geography.xlsx"

NUM = "#,##0"
PCT = "0%"
PCT1 = "0.0%"

# ---- canonical display labels from the original dept file (Title Case) ----
# dissemination stores geography lowercase; map to the exact strings the dept
# has already seen, falling back to .title() for any new geography.
disp_d, disp_m, disp_c = {}, {}, {}
ord_d, ord_dm, ord_dmc = [], [], []   # original row order, for exact match
try:
    ob = openpyxl.load_workbook(LABELS, read_only=True)
    for r in ob["DF By District"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL":
            disp_d[r[0].strip().lower()] = r[0].strip()
            ord_d.append(r[0].strip())
    for r in ob["DF By Mandal"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL" and r[1]:
            disp_d[r[0].strip().lower()] = r[0].strip()
            disp_m[r[1].strip().lower()] = r[1].strip()
            ord_dm.append((r[0].strip(), r[1].strip()))
    for r in ob["DF By Cluster"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL" and r[2]:
            disp_c[r[2].strip().lower()] = r[2].strip()
            ord_dmc.append((r[0].strip(), r[1].strip(), r[2].strip()))
    ob.close()
    print(f"display labels from original: {len(disp_d)} dist, "
          f"{len(disp_m)} mandal, {len(disp_c)} cluster")
except FileNotFoundError:
    print("original file not found; using .title() for labels")

import re
def _k(s): return re.sub(r"\s+", " ", s.strip().lower())
def dd(s): return disp_d.get(_k(s), re.sub(r"\s+", " ", s.strip()).title())
def dm(s): return disp_m.get(_k(s), re.sub(r"\s+", " ", s.strip()).title())
def dc(s): return disp_c.get(_k(s), re.sub(r"\s+", " ", s.strip()).title())

# ---- load geography: ppbno -> (district, mandal, cluster) display strings ----
geo = {}
with open(DISSEM, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        geo[row["ppbno"].strip()] = (
            dd(row["district_name"]),
            dm(row["mandal_name"]),
            dc(row["cluster_name"]),
        )
print(f"geography rows: {len(geo):,}")

# ---- latest anon CSV ----
cands = sorted(glob.glob(str(DATA / "farmers_delivery_status_anon_*.csv")),
               key=os.path.getmtime)
anon = Path(cands[-1])
print(f"anon source: {anon.name}")

# ---- aggregate status counts by geography level ----
# bucket = [sent, delivered, read, failed]   (sent absorbs no_status)
def newb(): return [0, 0, 0, 0]
by_d   = defaultdict(newb)
by_dm  = defaultdict(newb)
by_dmc = defaultdict(newb)
IDX = {"sent": 0, "delivered": 1, "read": 2, "failed": 3, "no_status": 0}
unmatched = 0
total_rows = 0
with open(anon, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        total_rows += 1
        g = geo.get(row["ppbno"].strip())
        if not g:
            unmatched += 1
            continue
        d, m, c = g
        i = IDX.get(row["last_status"], 0)
        by_d[d][i] += 1
        by_dm[(d, m)][i] += 1
        by_dmc[(d, m, c)][i] += 1
print(f"anon rows: {total_rows:,}  unmatched ppbno: {unmatched:,}")

wb = openpyxl.Workbook()
wb.remove(wb.active)
hdr_font = Font(bold=True)

def style_header(ws, ncols):
    for c in range(1, ncols + 1):
        cell = ws.cell(1, c)
        cell.font = hdr_font
        cell.alignment = Alignment(wrap_text=True, vertical="center")
    ws.freeze_panes = "A2"

def divider(name, a1=None):
    ws = wb.create_sheet(name)
    if a1 is not None:
        ws["A1"] = a1
    return ws

# ================= DeliveredFailed section =================
divider("DeliveredFailed")

def df_tab(name, keys, label_cols, getrow):
    """DF tab: label cols + Total Sent | Delivered | Failed | %Del | %Fail.
    Delivered = Total - Failed. % as computed values."""
    ws = wb.create_sheet(name)
    headers = label_cols + ["Total Sent", "Delivered", "Failed", "% Delivered", "% Failed"]
    ws.append(headers)
    nlab = len(label_cols)
    gt = newb()
    for k in keys:
        b = getrow(k)
        for i in range(4):
            gt[i] += b[i]
        sent, deliv, read, fail = b
        total = sent + deliv + read + fail
        delivered = total - fail
        labels = list(k) if isinstance(k, tuple) else [k]
        ws.append(labels + [total, delivered, fail,
                            (delivered / total if total else 0),
                            (fail / total if total else 0)])
    # TOTAL row
    s, d, r, fa = gt
    t = s + d + r + fa
    dv = t - fa
    ws.append((["TOTAL"] + [None] * (nlab - 1)) +
              [t, dv, fa, (dv / t if t else 0), (fa / t if t else 0)])
    # formats
    for row in ws.iter_rows(min_row=2):
        for c in range(nlab, nlab + 3):       # count cols
            row[c].number_format = NUM
        for c in range(nlab + 3, nlab + 5):   # pct cols
            row[c].number_format = PCT
    ws.cell(ws.max_row, 1).font = hdr_font
    style_header(ws, len(headers))
    return ws

def order_by(orig_seq, present):
    """Use the original file's row order for keys that exist; append any new
    keys (not in original) at the end, sorted, so nothing is dropped."""
    seen, out = set(), []
    for k in orig_seq:
        if k in present and k not in seen:
            out.append(k); seen.add(k)
    for k in sorted(present):
        if k not in seen:
            out.append(k); seen.add(k)
    return out

districts = order_by(ord_d, set(by_d))
mandals   = order_by(ord_dm, set(by_dm))
clusters  = order_by(ord_dmc, set(by_dmc))

df_tab("DF By District", districts, ["District"], lambda k: by_d[k])
df_tab("DF By Mandal", mandals, ["District", "Mandal"], lambda k: by_dm[k])
df_tab("DF By Cluster", clusters, ["District", "Mandal", "Cluster"], lambda k: by_dmc[k])

# ================= Full Breakdown section =================
divider("Full Breakdown")

def raw_tab(name, keys, label_cols, getrow, pct_fmt, read_hdr):
    """Raw tab: labels + Sent|Delivered|Read|Failed|Total|%Sent|%Del|%Read|%Fail.
    Total and % are live formulas, matching the original."""
    ws = wb.create_sheet(name)
    nlab = len(label_cols)
    headers = label_cols + ["Sent", "Delivered", "Read", "Failed", "Total",
                            "% Sent", "% Delivered", read_hdr, "% Failed"]
    ws.append(headers)
    # column letters
    from openpyxl.utils import get_column_letter
    cS = get_column_letter(nlab + 1)  # Sent
    cD = get_column_letter(nlab + 2)  # Delivered
    cR = get_column_letter(nlab + 3)  # Read
    cF = get_column_letter(nlab + 4)  # Failed
    cT = get_column_letter(nlab + 5)  # Total
    n_data = len(keys)
    for k in keys:
        b = getrow(k)
        labels = list(k) if isinstance(k, tuple) else [k]
        rr = ws.max_row + 1
        ws.append(labels + [b[0], b[1], b[2], b[3],
                            f"=SUM({cS}{rr}:{cF}{rr})",
                            f"=IFERROR({cS}{rr}/{cT}{rr},0)",
                            f"=IFERROR({cD}{rr}/{cT}{rr},0)",
                            f"=IFERROR({cR}{rr}/({cR}{rr}+{cD}{rr}),0)",
                            f"=IFERROR({cF}{rr}/{cT}{rr},0)"])
    # TOTAL row with SUM over data range, % via 0% format
    tr = ws.max_row + 1
    r0, r1 = 2, ws.max_row
    ws.append((["TOTAL"] + [None] * (nlab - 1)) + [
        f"=SUM({cS}{r0}:{cS}{r1})",
        f"=SUM({cD}{r0}:{cD}{r1})",
        f"=SUM({cR}{r0}:{cR}{r1})",
        f"=SUM({cF}{r0}:{cF}{r1})",
        f"=SUM({cT}{r0}:{cT}{r1})",
        f"=IFERROR({cS}{tr}/{cT}{tr},0)",
        f"=IFERROR({cD}{tr}/{cT}{tr},0)",
        f"=IFERROR({cR}{tr}/({cR}{tr}+{cD}{tr}),0)",
        f"=IFERROR({cF}{tr}/{cT}{tr},0)"])
    # formats
    for row in ws.iter_rows(min_row=2):
        for c in range(nlab, nlab + 5):       # Sent..Total
            row[c].number_format = NUM
        for c in range(nlab + 5, nlab + 9):   # % cols
            row[c].number_format = pct_fmt
    # TOTAL row % always integer 0%
    for c in range(nlab + 5, nlab + 9):
        ws.cell(tr, c + 1).number_format = PCT
    ws.cell(tr, 1).font = hdr_font
    style_header(ws, len(headers))
    return ws

raw_tab("By District", districts, ["District"], lambda k: by_d[k],
        PCT, "% Read\n(of Delivered)")
raw_tab("By Mandal", mandals, ["District", "Mandal"], lambda k: by_dm[k],
        PCT1, "% Read\n(of Delivered)")
raw_tab("By Cluster", clusters, ["District", "Mandal", "Cluster"], lambda k: by_dmc[k],
        PCT, "% Read\n(of delivered)")

# column widths
for ws in wb.worksheets:
    for col in ws.columns:
        first = col[0]
        if first.value:
            w = max(len(str(c.value)) for c in col[:3] if c.value) if len(col) else 10
            ws.column_dimensions[first.column_letter].width = max(12, min(w + 2, 26))

wb.save(OUT)
print(f"\nSaved -> {OUT}")
print(f"tabs: {wb.sheetnames}")
print(f"districts={len(districts)} mandals={len(mandals)} clusters={len(clusters)}")

# sanity print: DF district totals
gt = newb()
for d in districts:
    for i in range(4):
        gt[i] += by_d[d][i]
s, dl, r, fa = gt
t = s + dl + r + fa
print(f"GRAND: total_sent={t:,} delivered(not-failed)={t-fa:,} failed={fa:,} "
      f"(failed {100*fa/t:.1f}%)")
