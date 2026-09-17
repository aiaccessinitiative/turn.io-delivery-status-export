#!/usr/bin/env python3
"""Merge the weekly anon pulls into one wide farmer-by-week matrix.

turn_export_anon.py produces one CSV per week (long: one row per message).
This collapses N of those into a single wide CSV, one row per farmer, with a
block of columns per advisory week:

    ppbno, w1_variant, w1_arm, w1_status, w1_read, w1_status_ts,
           w2_variant, ...

By default the output is phone-free, like every other file this toolkit
writes: the weekly pulls are ppbno-keyed and nothing here needs a number.
With --geo it adds district/mandal/cluster from the dissemination roster, the
same way build_status_geo_file.py does.

If the pulls were made with `turn_export_anon.py --phone-key` (no roster, key
column is the recipient number), this script accepts them unchanged and the
first output column is `phone` instead of `ppbno`. --geo and --all-roster are
unavailable in that case since they join through the roster.

--with-phone is the other deliberate exception. It joins MobileNo back in from
the roster and writes it as a `phone` column beside ppbno. That is for an operator who
holds the roster anyway and needs the number next to the status. The file it
produces contains PII: keep it out of the repo, out of shared drives, and
delete it when the job is done.

Run fix_arms_and_verify.py on each weekly pull FIRST. The arm column straight
out of the puller is a rough default split and is not authoritative.

The join runs through a temporary SQLite database rather than a dict, because
~1.4M farmers x 4 weeks does not fit comfortably in memory.

Usage:
  python build_week_matrix.py \\
      --week w1=data/farmers_delivery_status_anon_20260530T....csv \\
      --week w2=data/farmers_delivery_status_anon_20260606T....csv \\
      --week w3=data/farmers_delivery_status_anon_20260613T....csv \\
      --week w4=data/farmers_delivery_status_anon_20260620T....csv \\
      --geo

  # write the preview only, then approve:
  python build_week_matrix.py --week ... --preview 200
  python build_week_matrix.py --week ... --approved

Read rates are a floor. A farmer who has read receipts switched off never
produces a `read` status, so `{w}_read` undercounts by an unknown amount.
Compare weeks only at matched days-after-send: read counts keep climbing for
a week or more after the send.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
import tempfile
from pathlib import Path

import config

SAFE_NAME = re.compile(r"^[a-z0-9_]+$")

# Furthest state reached wins when a farmer has more than one message in a
# week. Mirrors the read > delivered > sent ordering used across the toolkit.
STATUS_RANK = {"read": 4, "delivered": 3, "sent": 2, "failed": 1,
               "no_status": 0, "": 0}

PER_WEEK_COLUMNS = ["variant", "arm", "status", "read", "status_ts"]

SCHEMA = """
PRAGMA journal_mode = OFF;
PRAGMA synchronous = OFF;
PRAGMA temp_store = MEMORY;
PRAGMA cache_size = -262144;

CREATE TABLE weekly (
    week        TEXT NOT NULL,
    ppbno       TEXT NOT NULL,
    variant     TEXT,
    arm         TEXT,
    status      TEXT,
    rank        INTEGER,
    status_ts   TEXT,
    PRIMARY KEY (week, ppbno)
) WITHOUT ROWID;

CREATE TABLE farmers (ppbno TEXT PRIMARY KEY) WITHOUT ROWID;

CREATE TABLE geo (
    ppbno    TEXT PRIMARY KEY,
    district TEXT, mandal TEXT, cluster TEXT,
    phone    TEXT
) WITHOUT ROWID;
"""


def collapse(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def parse_week_arg(spec: str):
    name, sep, path = spec.partition("=")
    if not sep:
        raise SystemExit("Bad --week %r. Expected name=path" % spec)
    if not SAFE_NAME.match(name):
        raise SystemExit("Week name %r must match [a-z0-9_]+; it becomes a "
                         "column prefix." % name)
    return name, Path(path)


def load_week(conn, name: str, path: Path):
    """Returns (rows read, key column name) for one weekly pull."""
    if not path.exists():
        raise SystemExit("Not found: %s" % path)
    rows = 0
    batch = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        fields = reader.fieldnames or []
        # Pulls are keyed on ppbno by default, or on the recipient number when
        # turn_export_anon.py ran with --phone-key. Either works here.
        key_col = ("ppbno" if "ppbno" in fields
                   else "phone" if "phone" in fields else None)
        if key_col is None or "last_status" not in fields:
            raise SystemExit(
                "%s needs a ppbno or phone column plus last_status. Columns: %s"
                % (path.name, fields))
        has_ts = "last_status_timestamp" in fields
        has_label = "arm_label" in fields
        for row in reader:
            ppbno = (row.get(key_col) or "").strip()
            if not ppbno:
                continue
            rows += 1
            status = (row.get("last_status") or "no_status").strip().lower()
            batch.append((
                name, ppbno,
                collapse(row.get("variant")),
                collapse(row.get("arm_label") if has_label else row.get("arm")),
                status,
                STATUS_RANK.get(status, 0),
                (row.get("last_status_timestamp") or "").strip() if has_ts else "",
            ))
            if len(batch) >= 50000:
                upsert(conn, batch)
                batch = []
    if batch:
        upsert(conn, batch)
    conn.commit()
    return rows, key_col


def upsert(conn, batch) -> None:
    """Insert, keeping the furthest status when a ppbno repeats in a week.

    Duplicates are not counted here; main() reports them as the gap between
    rows read and rows stored, which is the same number and cheaper to get.
    """
    conn.executemany(
        "INSERT INTO weekly (week, ppbno, variant, arm, status, rank, status_ts) "
        "VALUES (?,?,?,?,?,?,?) "
        "ON CONFLICT(week, ppbno) DO UPDATE SET "
        "  variant   = CASE WHEN excluded.rank > weekly.rank THEN excluded.variant   ELSE weekly.variant   END, "
        "  arm       = CASE WHEN excluded.rank > weekly.rank THEN excluded.arm       ELSE weekly.arm       END, "
        "  status    = CASE WHEN excluded.rank > weekly.rank THEN excluded.status    ELSE weekly.status    END, "
        "  status_ts = CASE WHEN excluded.rank > weekly.rank THEN excluded.status_ts ELSE weekly.status_ts END, "
        "  rank      = MAX(excluded.rank, weekly.rank)",
        batch)


def load_roster(conn, with_phone: bool) -> int:
    """Load ppbno -> geography (and, only on request, the phone) from the
    dissemination roster. The phone column is left NULL unless with_phone."""
    dissem = config.require_dissem()
    print("loading roster (ppbno -> district/mandal/cluster%s)..."
          % (", phone" if with_phone else ""))
    batch = []
    count = 0
    with dissem.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if with_phone and "MobileNo" not in (reader.fieldnames or []):
            raise SystemExit("--with-phone needs a MobileNo column in the "
                             "roster. Columns: %s" % reader.fieldnames)
        for row in reader:
            ppbno = (row.get("ppbno") or "").strip()
            if not ppbno:
                continue
            phone = None
            if with_phone:
                phone = re.sub(r"\D", "", row.get("MobileNo") or "") or None
            batch.append((ppbno,
                          collapse(row.get("district_name")).title(),
                          collapse(row.get("mandal_name")).title(),
                          collapse(row.get("cluster_name")).title(),
                          phone))
            if len(batch) >= 50000:
                conn.executemany("INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?)",
                                 batch)
                count += len(batch)
                batch = []
    if batch:
        conn.executemany("INSERT OR REPLACE INTO geo VALUES (?,?,?,?,?)", batch)
        count += len(batch)
    conn.commit()
    return count


def build_universe(conn, use_roster: bool) -> int:
    if use_roster:
        conn.execute("INSERT OR IGNORE INTO farmers SELECT ppbno FROM geo")
    conn.execute("INSERT OR IGNORE INTO farmers SELECT DISTINCT ppbno FROM weekly")
    conn.commit()
    return conn.execute("SELECT COUNT(*) FROM farmers").fetchone()[0]


def build_query(weeks, with_geo: bool, with_phone: bool = False,
                key_name: str = "ppbno") -> str:
    selects = ["f.ppbno AS %s" % key_name]
    joins = []
    if with_geo or with_phone:
        joins.append("LEFT JOIN geo g ON g.ppbno = f.ppbno")
    if with_phone:
        selects.append("COALESCE(g.phone,'') AS phone")
    if with_geo:
        selects += ["COALESCE(g.district,'') AS district",
                    "COALESCE(g.mandal,'') AS mandal",
                    "COALESCE(g.cluster,'') AS cluster"]
    for week in weeks:
        alias = "w_" + week
        joins.append("LEFT JOIN weekly %s ON %s.ppbno = f.ppbno AND %s.week = '%s'"
                     % (alias, alias, alias, week))
        selects += [
            "COALESCE(%s.variant,'') AS %s_variant" % (alias, week),
            "COALESCE(%s.arm,'') AS %s_arm" % (alias, week),
            "COALESCE(%s.status,'not_in_pull') AS %s_status" % (alias, week),
            "CASE WHEN %s.status = 'read' THEN 1 ELSE 0 END AS %s_read"
            % (alias, week),
            "COALESCE(%s.status_ts,'') AS %s_status_ts" % (alias, week),
        ]
    return ("SELECT " + ",\n       ".join(selects)
            + "\n  FROM farmers f\n  " + "\n  ".join(joins)
            + "\n ORDER BY f.ppbno")


def write_csv(conn, weeks, out_path: Path, with_geo: bool, limit: int,
              with_phone: bool = False, key_name: str = "ppbno") -> int:
    query = build_query(weeks, with_geo, with_phone, key_name)
    if limit:
        query += "\n LIMIT %d" % limit
    cur = conn.execute(query)
    header = [d[0] for d in cur.description]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    with out_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        while True:
            chunk = cur.fetchmany(20000)
            if not chunk:
                break
            writer.writerows(chunk)
            written += len(chunk)
            if written % 200000 == 0:
                print("  %s rows" % format(written, ","))
    return written


def summarise(conn, weeks):
    print("\nPer-week status mix (one row per farmer per week):")
    for week in weeks:
        rows = conn.execute(
            "SELECT status, COUNT(*) FROM weekly WHERE week = ? "
            " GROUP BY status ORDER BY COUNT(*) DESC", (week,)).fetchall()
        total = sum(c for _, c in rows)
        if not total:
            print("  %-6s no rows" % week)
            continue
        parts = ", ".join("%s %s (%.1f%%)"
                          % (s, format(c, ","), 100.0 * c / total)
                          for s, c in rows)
        reached = sum(c for s, c in rows if s in ("read", "delivered"))
        read = sum(c for s, c in rows if s == "read")
        print("  %-6s %s farmers | %s" % (week, format(total, ","), parts))
        print("         read of reached: %.1f%%"
              % (100.0 * read / reached if reached else 0.0))

        variants = conn.execute(
            "SELECT variant, COUNT(*) FROM weekly WHERE week = ? "
            " GROUP BY variant ORDER BY COUNT(*) DESC LIMIT 6", (week,)).fetchall()
        print("         variants: %s%s"
              % (", ".join("%s=%s" % (v or "(blank)", format(c, ","))
                           for v, c in variants),
                 " ..." if len(variants) == 6 else ""))

    present = conn.execute(
        "SELECT COUNT(*) FROM (SELECT ppbno FROM weekly GROUP BY ppbno "
        "                       HAVING COUNT(DISTINCT week) = ?)",
        (len(weeks),)).fetchone()[0]
    everyone = conn.execute("SELECT COUNT(*) FROM farmers").fetchone()[0]
    print("\n%s of %s farmers appear in all %d weeks"
          % (format(present, ","), format(everyone, ","), len(weeks)))


def print_preview(path: Path, n: int):
    print("\n" + "=" * 72)
    print("PREVIEW: first %d rows written to %s" % (n, path.name))
    print("=" * 72)
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for i, line in enumerate(fh):
            if i > 6:
                break
            print("  " + line.rstrip()[:220])
    print("\nWhat to check before approving:")
    print("  * variant names are this week's, not a neighbouring week's")
    print("  * arm labels look right (run fix_arms_and_verify.py first)")
    print("  * status mix is plausible: ~29% failed has been stable")
    print("  * status timestamps are the event times, not the pull time")


def parse_args():
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--week", action="append", required=True, metavar="NAME=CSV",
                   help="One weekly anon pull (repeatable, order preserved).")
    p.add_argument("--out", type=Path, default=None,
                   help="Output CSV. Default data/farmer_week_matrix.csv, or "
                        "data/farmer_week_matrix_with_phone.csv when the file "
                        "will contain phone numbers.")
    p.add_argument("--geo", action="store_true",
                   help="Add district/mandal/cluster from the roster.")
    p.add_argument("--all-roster", action="store_true",
                   help="Include every ppbno in the roster, so farmers who "
                        "were never messaged still get a row. Implies --geo.")
    p.add_argument("--with-phone", action="store_true",
                   help="Add the farmer's MobileNo from the roster as a "
                        "`phone` column. The output then contains PII; keep "
                        "it out of the repo and shared drives.")
    p.add_argument("--preview", type=int, default=200,
                   help="Write this many rows first and stop for review "
                        "(default 200). 0 disables.")
    p.add_argument("--approved", action="store_true",
                   help="Skip the preview stop and write the full file.")
    p.add_argument("--limit-rows", type=int, default=0,
                   help="Write only N rows (debugging).")
    p.add_argument("--keep-db", action="store_true",
                   help="Keep the staging SQLite file for ad-hoc queries.")
    return p.parse_args()


def main():
    args = parse_args()
    if args.all_roster:
        args.geo = True

    pairs = [parse_week_arg(spec) for spec in args.week]
    weeks = [name for name, _ in pairs]
    if len(set(weeks)) != len(weeks):
        raise SystemExit("Duplicate week names: %s" % weeks)

    stage_path = Path(tempfile.mkstemp(prefix="week_matrix_", suffix=".db",
                                       dir=str(config.DATA_DIR))[1])
    conn = sqlite3.connect(stage_path)
    conn.executescript(SCHEMA)

    try:
        key_cols = set()
        for name, path in pairs:
            rows, key_col = load_week(conn, name, path)
            key_cols.add(key_col)
            kept = conn.execute("SELECT COUNT(*) FROM weekly WHERE week = ?",
                                (name,)).fetchone()[0]
            note = ("" if kept == rows
                    else "  (%s duplicate %s rows collapsed to the furthest "
                         "status)" % (format(rows - kept, ","), key_col))
            print("%-6s %s rows from %s%s"
                  % (name, format(rows, ","), path.name, note))

        if len(key_cols) != 1:
            raise SystemExit("Weekly pulls are keyed differently (%s). Pull "
                             "every week the same way." % sorted(key_cols))
        key_name = key_cols.pop()
        if key_name == "phone":
            if args.geo or args.all_roster:
                raise SystemExit("--geo and --all-roster join through the "
                                 "roster on ppbno, but these pulls are "
                                 "phone-keyed (--phone-key). Drop those flags.")
            if args.with_phone:
                print("note: pulls are already phone-keyed, --with-phone is "
                      "redundant and ignored.")
                args.with_phone = False

        # Name the file after what it contains, so a phone-bearing CSV is
        # never mistaken for an anonymised one.
        has_phone = key_name == "phone" or args.with_phone
        if args.out is None:
            args.out = config.DATA_DIR / ("farmer_week_matrix_with_phone.csv"
                                          if has_phone else "farmer_week_matrix.csv")

        if args.geo or args.with_phone:
            print("roster: %s ppbno rows"
                  % format(load_roster(conn, args.with_phone), ","))
        if has_phone:
            print("NOTE: %s will contain phone numbers. Keep it out of the "
                  "repo and shared drives." % args.out.name)

        total = build_universe(conn, args.all_roster)
        print("universe: %s farmers" % format(total, ","))

        if args.preview and not args.approved and not args.limit_rows:
            preview_path = args.out.with_suffix(".preview.csv")
            written = write_csv(conn, weeks, preview_path, args.geo,
                                args.preview, args.with_phone, key_name)
            print("\nwrote %s preview rows to %s"
                  % (format(written, ","), preview_path))
            print_preview(preview_path, written)
            summarise(conn, weeks)
            approved = False
            if sys.stdin is not None and sys.stdin.isatty():
                try:
                    approved = input("\nWrite the full matrix? [y/N] "
                                     ).strip().lower() in ("y", "yes")
                except EOFError:
                    approved = False
            else:
                print("\nNot a TTY, so stopping after the preview.")
            if not approved:
                print("Re-run with --approved to write all %s rows."
                      % format(total, ","))
                return

        written = write_csv(conn, weeks, args.out, args.geo, args.limit_rows,
                            args.with_phone, key_name)
        print("\nwrote %s rows to %s" % (format(written, ","), args.out))
        summarise(conn, weeks)
    finally:
        conn.close()
        if args.keep_db:
            print("staging db kept at %s" % stage_path)
        else:
            try:
                stage_path.unlink()
            except OSError:
                pass


if __name__ == "__main__":
    main()
