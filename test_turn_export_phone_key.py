#!/usr/bin/env python3
"""Offline test of turn_export_anon.pull_window with and without --phone-key.

Monkeypatches the HTTP layer with two fake pages shaped like the live Data
Export API (verified 2026-09-17), then checks what lands in the window CSV:

  roster mode   : one matched farmer written as ppbno, one unmatched dropped
  --phone-key   : both written, keyed by the full recipient number, no roster

No network, no token, no real data.

  python test_turn_export_phone_key.py
"""
from __future__ import annotations

import csv
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import turn_export_anon as tx  # noqa: E402


def msg(to, camp, status, ts=1780624800, direction="outbound", mtype="template"):
    return {
        "from": "912250972990", "to": to, "id": "wamid.FAKE", "type": mtype,
        "timestamp": str(ts),
        "_vnd": {"v1": {"direction": direction, "author": {"name": camp},
                        "last_status": status,
                        "last_status_timestamp": "2026-06-05T02:08:31.000000Z"}},
    }


PAGES = {
    "c1": {"data": [
        msg("919000000001", "var4", "read"),           # in roster
        msg("919000000002", "var4", "failed"),         # NOT in roster
        msg("919000000003", "Promo arm 1", "read"),    # not a var* campaign
    ], "paging": {"next": "c2"}},
    "c2": {"data": [
        {"messages": [msg("919000000004", "var7", "delivered",
                          direction="inbound", mtype="text")]},  # inbound, skip
        msg("919000000005", "0528_var9", "sent"),      # in roster, week-1 prefix
        msg("919000000006", "0611_var3", "delivered"), # other date prefix, keep
        msg("919000000007", "var12_test", "read"),     # not a campaign name, drop
    ], "paging": {}},
}


def fake_http(url, tok, limiter, body=None):
    if body is not None:
        return {"cursor": "c1"}
    return PAGES[url.rsplit("/", 1)[-1]]


tx.http = fake_http

# The puller keys the roster on the LAST 10 digits of MobileNo, so
# "919000000001" -> "9000000001".
ROSTER = {"9000000001": "PB001", "9000000005": "PB005"}
COLS_PPBNO = ["ppbno", "variant", "arm", "arm_label", "last_status",
              "timestamp", "last_status_timestamp"]
COLS_PHONE = ["phone"] + COLS_PPBNO[1:]


def run_window(phone_map, cols, root: Path):
    resume = root / ("resume_" + cols[0])
    resume.mkdir()
    n = tx.pull_window(0, "2026-06-05T02:00:00.000Z", "2026-06-05T03:00:00.000Z",
                       "tok", tx.RateLimiter(10000), phone_map, resume, cols, {})
    with (resume / "win_000.csv").open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh, fieldnames=cols))
    unmatched = int((resume / "win_000.done").read_text().split()[1])
    return n, rows, unmatched


def main():
    failures = []

    def check(label, actual, expected):
        if actual != expected:
            failures.append("%s: expected %r, got %r" % (label, expected, actual))

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        n, rows, unm = run_window(ROSTER, COLS_PPBNO, root)
        check("roster mode rows written", n, 2)
        # ...0002 (var4) and ...0006 (0611_var3) are var sends not in the roster
        check("roster mode unmatched counted", unm, 2)
        check("roster mode keys", sorted(r["ppbno"] for r in rows), ["PB001", "PB005"])
        check("roster mode never writes a phone",
              any("9190000" in v for r in rows for v in r.values()), False)
        check("roster mode variant", rows[0]["variant"], "var4")
        check("roster mode status", rows[0]["last_status"], "read")

        n, rows, unm = run_window(None, COLS_PHONE, root)
        check("phone-key rows written", n, 4)
        check("phone-key unmatched is zero", unm, 0)
        check("phone-key keys", sorted(r["phone"] for r in rows),
              ["919000000001", "919000000002", "919000000005", "919000000006"])
        check("any MMDD_ date prefix on varN is kept",
              any(r["variant"] == "0611_var3" for r in rows), True)
        check("varN with a suffix is not a campaign",
              any(r["variant"] == "var12_test" for r in rows), False)
        check("phone-key keeps the unmatched farmer's status",
              next(r for r in rows if r["phone"] == "919000000002")["last_status"],
              "failed")
        check("phone-key still drops promo", any("Promo" in r["variant"] for r in rows), False)
        check("phone-key still drops inbound", any(r["phone"] == "919000000004" for r in rows), False)
        check("phone-key send timestamp is ISO",
              rows[0]["timestamp"].startswith("2026-06-05T02:00:00"), True)

    # --- adaptive rate limiter -------------------------------------------
    lim = tx.RateLimiter(55, adaptive=True)
    check("limiter starts at 55", lim.limit, 55)
    lim.observe("60")
    check("60/min header -> paces at 55 (92%)", lim.limit, 55)
    lim.observe("120")
    check("120/min header -> paces at 110", lim.limit, 110)
    lim.observe("garbage"); lim.observe(None); lim.observe("0")
    check("bad headers ignored", lim.limit, 110)
    lim.observe("30")
    check("lowered quota respected", lim.limit, 27)
    pinned = tx.RateLimiter(40, adaptive=False)
    pinned.observe("120")
    check("--rate-limit pin ignores header", pinned.limit, 40)

    print("\n".join("  " + f for f in failures) if failures else
          "PASSED: pull_window roster mode, --phone-key mode, adaptive limiter")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
