#!/usr/bin/env python3
"""Post-process the latest anon export and verify it against the forecast table.

  1. Recompute arm / arm_label from the variant number. The pull's `variant`
     column is authoritative; the `arm` column is only derived, so no re-pull
     is needed to fix it.
  2. Print a per-variant verification table vs the forecast's expected sends
     (counts, %, read-of-reached), plus per-arm delivery/read rates.

>>> EDIT PER CAMPAIGN <<<  The arm split, variant count, and expected-send
counts change EVERY week. Before each run, update: arm_for() (the split),
vnum() (any renamed variants, e.g. a video variant not named varN), and the
EXPECTED dict (the forecast counts). The block below is the last campaign's
values as a worked example.

Overwrites the target CSV in place (only the two derived arm columns change).
"""
import csv, glob, os, sys
from collections import defaultdict
from pathlib import Path

import config
DATA = config.DATA_DIR
ARM_LABEL = {"1": "WA Text", "2": "WA Text+Image", "3": "WA Bundle"}

def arm_for(vn: int) -> str:
    # WEEK 4 (Jun 19 Reminders): 5/6/6 — var1-5 Text, var6-11 Image, var12-17 Bundle
    return "1" if vn <= 5 else "2" if vn <= 11 else "3"

def vnum(v: str) -> int:
    # Turn named variant 12 (the big "uncertain" video) "uncertain_vid"; map back.
    if "uncertain_vid" in v.lower():
        return 12
    digits = "".join(ch for ch in v if ch.isdigit())
    return int(digits) if digits else 0

# Week-4 (Jun 19 Reminders) expected SENT counts per variant, var1-17
EXPECTED = {
    1: 147683, 2: 3261, 3: 1707, 4: 20536, 5: 18110,
    6: 147215, 7: 9289, 8: 2636, 9: 21407, 10: 9816, 11: 941,
    12: 602802, 13: 26725, 14: 13384, 15: 84397, 16: 102309, 17: 1513,
}
NVAR = len(EXPECTED)  # 17 this week

def main():
    target = sys.argv[1] if len(sys.argv) > 1 else None
    if not target:
        cands = sorted(glob.glob(str(DATA / "farmers_delivery_status_anon_*.csv")),
                       key=os.path.getmtime)
        if not cands:
            sys.exit("No anon CSV found in data/")
        target = cands[-1]
    target = Path(target)
    print(f"Target: {target.name}")

    rows = []
    counts = defaultdict(int)
    status_by_variant = defaultdict(lambda: defaultdict(int))
    with open(target, encoding="utf-8-sig", newline="") as f:
        r = csv.DictReader(f)
        cols = r.fieldnames
        for row in r:
            vn = vnum(row["variant"])
            row["arm"] = arm_for(vn)
            row["arm_label"] = ARM_LABEL[row["arm"]]
            rows.append(row)
            counts[vn] += 1
            status_by_variant[vn][row["last_status"]] += 1

    # rewrite in place with corrected arm columns
    with open(target, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)

    # verification table
    print(f"\n{'var':>4} {'arm':>4} {'pulled':>10} {'expected':>10} {'diff':>8} {'read%':>7}")
    tot_p = tot_e = 0
    arm_tot = defaultdict(int)
    for vn in range(1, NVAR + 1):
        p = counts.get(vn, 0)
        e = EXPECTED[vn]
        tot_p += p; tot_e += e
        arm = arm_for(vn)
        arm_tot[arm] += p
        sc = status_by_variant[vn]
        read = sc.get("read", 0)
        delivered = sc.get("delivered", 0)
        reached = read + delivered
        readpct = round(100 * read / reached, 1) if reached else 0.0
        print(f"{vn:>4} {arm:>4} {p:>10,} {e:>10,} {p-e:>+8,} {readpct:>6}%")
    print(f"{'TOT':>4} {'':>4} {tot_p:>10,} {tot_e:>10,} {tot_p-tot_e:>+8,}")
    # A small net shortfall (~2K) is expected: some farmers opted out of
    # messaging and were never sent to. Flag only variants well below target.
    net = tot_p - tot_e
    print(f"\nNet diff: {net:+,} "
          f"({'within ~2K opt-out tolerance' if -3000 <= net <= 100 else 'CHECK: outside opt-out tolerance'})")
    # percentage-based flag: opt-outs hit the whole base ~uniformly, so only
    # flag a variant whose shortfall is materially worse than the fleet rate.
    fleet_pct = (tot_e - tot_p) / tot_e * 100
    flagged = []
    for vn in range(1, NVAR + 1):
        e = EXPECTED[vn]
        gap_pct = (e - counts.get(vn, 0)) / e * 100
        if gap_pct > fleet_pct + 0.5:  # >0.5pp worse than fleet opt-out rate
            flagged.append((vn, gap_pct))
    print(f"\nFleet shortfall: {fleet_pct:.2f}% (opt-outs). "
          + ("Variants materially worse: "
             + ", ".join(f"var{v} ({g:.2f}%)" for v, g in flagged)
             if flagged else "No variant materially worse than fleet rate."))

    # status breakdown by arm (the media-type delivery/read question)
    arm_st = defaultdict(lambda: defaultdict(int))
    for vn in range(1, NVAR + 1):
        a = arm_for(vn)
        for st, c in status_by_variant[vn].items():
            arm_st[a][st] += c
    print(f"\n{'arm':<18}{'total':>10}{'failed%':>9}{'reached%':>9}{'read%(reach)':>13}")
    for a in ("1", "2", "3"):
        s = arm_st[a]
        tot = sum(s.values())
        failed = s.get("failed", 0)
        read = s.get("read", 0)
        delivered = s.get("delivered", 0)
        reached = read + delivered
        if not tot:
            continue
        print(f"{ARM_LABEL[a]:<18}{tot:>10,}{100*failed/tot:>8.1f}%"
              f"{100*reached/tot:>8.1f}%{100*read/reached if reached else 0:>12.1f}%")
    print(f"\nArm totals (pulled):")
    print(f"  arm1 WA Text       : {arm_tot['1']:>10,}  (expected 191,297)")
    print(f"  arm2 WA Text+Image : {arm_tot['2']:>10,}  (expected 191,304)")
    print(f"  arm3 WA Bundle     : {arm_tot['3']:>10,}  (expected 831,130)")

if __name__ == "__main__":
    main()
