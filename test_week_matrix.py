#!/usr/bin/env python3
"""Smoke test for build_week_matrix.py.

Generates four fake weekly pulls plus a fake dissemination roster in a temp
directory, runs the merger, and asserts the wide CSV. No network, no real data.

  python test_week_matrix.py
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MERGER = HERE / "build_week_matrix.py"

WEEKS = ["w1", "w2", "w3", "w4"]
PULL_COLS = ["ppbno", "variant", "arm", "arm_label", "last_status",
             "timestamp", "last_status_timestamp"]

# ppbno -> week -> (variant, arm_label, last_status)
PLAN = {
    "PB001": {"w1": ("var14", "WA Bundle", "read"),
              "w2": ("var14", "WA Bundle", "read"),
              "w3": ("var14", "WA Bundle", "delivered"),
              "w4": ("var14", "WA Bundle", "read")},
    "PB002": {"w1": ("var2", "WA Text", "delivered"),
              "w2": ("var2", "WA Text", "failed"),
              "w3": ("var2", "WA Text", "sent"),
              "w4": ("var2", "WA Text", "no_status")},
    # PB003 is absent from w3 entirely.
    "PB003": {"w1": ("var7", "WA Text+Image", "read"),
              "w2": ("var7", "WA Text+Image", "delivered"),
              "w4": ("var7", "WA Text+Image", "failed")},
}
# PB004 is in the roster only; it appears with --all-roster.
ROSTER_ONLY = ["PB004"]

GEO = {
    "PB001": ("nalgonda", "kattangur", "cluster a"),
    "PB002": ("NALGONDA", "Chityal", "Cluster B"),
    "PB003": ("khammam", "madhira", "cluster c"),
    "PB004": ("khammam", "madhira", "cluster c"),
}


def write_fixtures(root: Path):
    data = root / "data"
    data.mkdir()

    for w_index, week in enumerate(WEEKS):
        path = data / ("pull_%s.csv" % week)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=PULL_COLS)
            writer.writeheader()
            for ppbno, per_week in PLAN.items():
                if week not in per_week:
                    continue
                variant, arm_label, status = per_week[week]
                ts = "2026-06-%02dT09:00:00.000Z" % (5 + w_index * 7)
                writer.writerow({
                    "ppbno": ppbno, "variant": variant,
                    "arm": arm_label[-1], "arm_label": arm_label,
                    "last_status": status, "timestamp": ts,
                    "last_status_timestamp": ts,
                })
            # PB001 gets a duplicate row in w2 with a weaker status. The
            # merger must keep the furthest state (read), not the last row.
            if week == "w2":
                writer.writerow({
                    "ppbno": "PB001", "variant": "var14", "arm": "3",
                    "arm_label": "WA Bundle", "last_status": "sent",
                    "timestamp": "2026-06-12T09:00:00.000Z",
                    "last_status_timestamp": "2026-06-12T09:00:00.000Z",
                })

    # Phone-keyed pulls, as written by turn_export_anon.py --phone-key: same
    # rows, but the key column is `phone` holding the recipient number.
    phone_of = {p: "9190000000%02d" % i for i, p in enumerate(GEO)}
    for w_index, week in enumerate(WEEKS):
        path = data / ("pullphone_%s.csv" % week)
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=["phone"] + PULL_COLS[1:])
            writer.writeheader()
            for ppbno, per_week in PLAN.items():
                if week not in per_week:
                    continue
                variant, arm_label, status = per_week[week]
                ts = "2026-06-%02dT09:00:00.000Z" % (5 + w_index * 7)
                writer.writerow({
                    "phone": phone_of[ppbno], "variant": variant,
                    "arm": arm_label[-1], "arm_label": arm_label,
                    "last_status": status, "timestamp": ts,
                    "last_status_timestamp": ts,
                })

    roster = root / "roster.csv"
    with roster.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["MobileNo", "ppbno", "district_name", "mandal_name",
                         "cluster_name", "village_name"])
        for i, (ppbno, (district, mandal, cluster)) in enumerate(GEO.items()):
            writer.writerow(["9190000000%02d" % i, ppbno, district, mandal,
                             cluster, "village"])
    return data, roster


def run(cmd, env, label):
    proc = subprocess.run(cmd, env=env, capture_output=True, text=True)
    print("\n$ %s" % label)
    for line in (proc.stdout or "").splitlines():
        print("  | " + line)
    if proc.returncode != 0:
        for line in (proc.stderr or "").splitlines()[-20:]:
            print("  ! " + line, file=sys.stderr)
    return proc


def read_rows(path: Path):
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return {r["ppbno"]: r for r in csv.DictReader(fh)}


def main():
    failures = []

    def check(label, actual, expected):
        if actual != expected:
            failures.append("%s: expected %r, got %r" % (label, expected, actual))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data, roster = write_fixtures(root)

        env = dict(os.environ)
        env["TURN_TOKEN"] = "unused-by-this-script"
        env["TURN_DATA_DIR"] = str(data)
        env["TURN_DISSEM_PATH"] = str(roster)

        base = [sys.executable, str(MERGER)]
        for week in WEEKS:
            base += ["--week", "%s=%s" % (week, data / ("pull_%s.csv" % week))]

        # --- preview gate ------------------------------------------------
        out = data / "matrix.csv"
        proc = run(base + ["--out", str(out), "--geo"], env, "preview run")
        check("preview exit code", proc.returncode, 0)
        preview = out.with_suffix(".preview.csv")
        check("preview file written", preview.exists(), True)
        check("full file not written yet", out.exists(), False)
        # Either path is correct: no TTY means it stops outright, a TTY means
        # it prompts and an unanswered prompt also stops.
        check("preview run stopped before writing the full file",
              ("stopping after the preview" in (proc.stdout or "")
               or "Re-run with --approved" in (proc.stdout or "")), True)

        # --- approved run ------------------------------------------------
        proc = run(base + ["--out", str(out), "--geo", "--approved"], env,
                   "approved run")
        check("approved exit code", proc.returncode, 0)
        rows = read_rows(out)

        check("row count (pulled farmers only)", len(rows), 3)
        check("PB001 w1 status", rows["PB001"]["w1_status"], "read")
        check("PB001 w1 read flag", rows["PB001"]["w1_read"], "1")
        check("PB001 w3 status", rows["PB001"]["w3_status"], "delivered")
        check("PB001 w3 read flag", rows["PB001"]["w3_read"], "0")
        check("duplicate collapses to furthest status",
              rows["PB001"]["w2_status"], "read")
        check("PB002 w2 failed", rows["PB002"]["w2_status"], "failed")
        check("PB002 w4 no_status", rows["PB002"]["w4_status"], "no_status")
        check("PB003 missing week", rows["PB003"]["w3_status"], "not_in_pull")
        check("PB003 missing week variant blank",
              rows["PB003"]["w3_variant"], "")
        check("variant carried through", rows["PB003"]["w1_variant"], "var7")
        check("arm label carried through",
              rows["PB001"]["w1_arm"], "WA Bundle")
        check("geography title-cased", rows["PB002"]["district"], "Nalgonda")
        check("no phone column", "MobileNo" in rows["PB001"], False)
        check("no phone-ish column",
              any("phone" in k.lower() or "mobile" in k.lower()
                  for k in rows["PB001"]), False)

        # --- opt-in phone column -----------------------------------------
        out_phone = data / "matrix_phone.csv"
        proc = run(base + ["--out", str(out_phone), "--with-phone",
                           "--approved"], env, "with-phone run")
        check("with-phone exit code", proc.returncode, 0)
        check("with-phone warns on stdout",
              "will contain phone numbers" in (proc.stdout or ""), True)

        # Default output name must say what the file contains.
        proc = run(base + ["--with-phone", "--approved"], env,
                   "with-phone run, default --out")
        check("default phone output exit code", proc.returncode, 0)
        check("default phone output is named *_with_phone.csv",
              (data / "farmer_week_matrix_with_phone.csv").exists(), True)
        proc = run(base + ["--approved"], env, "anon run, default --out")
        check("default anon output is farmer_week_matrix.csv",
              (data / "farmer_week_matrix.csv").exists(), True)
        phone_rows = read_rows(out_phone)
        check("phone column present", "phone" in phone_rows["PB001"], True)
        check("phone joined from roster (PB002 is roster row 1)",
              phone_rows["PB002"].get("phone"), "919000000001")
        check("phone run has no geo columns without --geo",
              "district" in phone_rows["PB001"], False)

        # --- phone-keyed pulls (turn_export_anon.py --phone-key) ----------
        out_pk = data / "matrix_phonekey.csv"
        cmd = [sys.executable, str(MERGER), "--out", str(out_pk), "--approved",
               "--with-phone"]   # redundant here; must be ignored, not fatal
        for week in WEEKS:
            cmd += ["--week", "%s=%s" % (week, data / ("pullphone_%s.csv" % week))]
        env_noroster = dict(env)
        env_noroster.pop("TURN_DISSEM_PATH", None)   # no roster at all
        proc = run(cmd, env_noroster, "phone-keyed run (no roster)")
        check("phone-keyed exit code", proc.returncode, 0)
        check("phone-keyed notes redundant flag",
              "already phone-keyed" in (proc.stdout or ""), True)
        with out_pk.open(encoding="utf-8-sig", newline="") as fh:
            pk_reader = csv.DictReader(fh)
            pk_header = pk_reader.fieldnames
            pk_rows = {r["phone"]: r for r in pk_reader}
        check("phone-keyed first column", pk_header[0], "phone")
        check("phone-keyed has no ppbno column", "ppbno" in pk_header, False)
        check("phone-keyed row count", len(pk_rows), 3)
        check("phone-keyed PB001 w1 status",
              pk_rows["919000000000"]["w1_status"], "read")
        check("phone-keyed missing week",
              pk_rows["919000000002"]["w3_status"], "not_in_pull")
        proc = run(cmd + ["--geo"], env_noroster, "phone-keyed + --geo (must refuse)")
        check("phone-keyed refuses --geo", proc.returncode != 0, True)

        # --- roster universe ---------------------------------------------
        out_all = data / "matrix_all.csv"
        proc = run(base + ["--out", str(out_all), "--all-roster", "--approved"],
                   env, "all-roster run")
        check("all-roster exit code", proc.returncode, 0)
        all_rows = read_rows(out_all)
        check("all-roster row count", len(all_rows), 4)
        check("roster-only farmer present", "PB004" in all_rows, True)
        if "PB004" in all_rows:
            check("roster-only farmer w1", all_rows["PB004"]["w1_status"],
                  "not_in_pull")

    print("\n" + "=" * 60)
    if failures:
        print("FAILED (%d)" % len(failures))
        for f in failures:
            print("  " + f)
    else:
        print("PASSED: all week-matrix assertions")
    print("=" * 60)
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
