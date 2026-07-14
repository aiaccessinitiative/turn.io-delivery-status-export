#!/usr/bin/env python3
"""Draw a random sample of N farmers per mandal from the dissemination roster.

Outputs `district, mandal, ppbno` (phone-free). Groups by (district, mandal) so
same-named mandals in different districts are sampled independently. Reproducible
via a fixed seed. Mandals with fewer than N farmers contribute all of theirs.

Usage:
  python sample_farmers_per_mandal.py            # 100 per mandal, seed 42
  python sample_farmers_per_mandal.py 200        # 200 per mandal
  python sample_farmers_per_mandal.py 200 7      # 200 per mandal, seed 7
"""
import csv, random, re, sys
from collections import defaultdict
import openpyxl

import config

N = int(sys.argv[1]) if len(sys.argv) > 1 else 100
SEED = int(sys.argv[2]) if len(sys.argv) > 2 else 42
random.seed(SEED)
DISSEM = config.require_dissem()
LABELS = config.REFERENCE_DIR / "geography_labels.xlsx"
OUT = config.DATA_DIR / f"sample_{N}_per_mandal_ppbno.csv"

def collapse(s): return re.sub(r"\s+", " ", s.strip())
def _k(s): return collapse(s).lower()

dd, dm = {}, {}
try:
    ob = openpyxl.load_workbook(LABELS, read_only=True)
    for r in ob["DF By Mandal"].iter_rows(min_row=2, values_only=True):
        if r[0] and r[0] != "TOTAL" and r[1]:
            dd[_k(r[0])] = collapse(r[0]); dm[_k(r[1])] = collapse(r[1])
    ob.close()
except FileNotFoundError:
    pass  # no reference labels -> fall back to .title()
D = lambda s: dd.get(_k(s), collapse(s).title())
M = lambda s: dm.get(_k(s), collapse(s).title())

groups = defaultdict(list); seen = set()
with open(DISSEM, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        p = row["ppbno"].strip()
        if not p or p in seen:
            continue
        seen.add(p)
        groups[(row["district_name"].strip().lower(), row["mandal_name"].strip().lower())].append(p)

total = 0; small = []
with open(OUT, "w", encoding="utf-8-sig", newline="") as fo:
    w = csv.DictWriter(fo, fieldnames=["district", "mandal", "ppbno"])
    w.writeheader()
    for key in sorted(groups):
        pool = groups[key]
        k = min(N, len(pool))
        if len(pool) < N:
            small.append((D(key[0]), M(key[1]), len(pool)))
        for p in sorted(random.sample(pool, k)):
            w.writerow({"district": D(key[0]), "mandal": M(key[1]), "ppbno": p})
            total += 1

print(f"{len(groups)} mandals | {total:,} rows (N={N}, seed={SEED}) -> {OUT}")
if small:
    print(f"{len(small)} mandal(s) had <{N} farmers (took all):")
    for d, m, c in small:
        print(f"  {d} / {m}: {c}")
