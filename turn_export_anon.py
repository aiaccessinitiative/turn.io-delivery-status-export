#!/usr/bin/env python3
"""Turn.io anonymised delivery-status export.

Pulls WhatsApp campaign messages for a time window from the Turn.io Data Export
API and writes an anonymised, ppbno-keyed CSV. Phone numbers are used only as an
in-memory join key (via the dissemination roster) and are NEVER written to disk.

Modes:
  (default)          pull the A/B variant sends (campaign names matching
                     VARIANT_RE, e.g. var1..varN / 0528_varN / uncertain_vid)
  --campaign-regex   pull a different campaign by name (e.g. '^promo arm \\d')
  --inbound          pull farmer-sent inbound TEXT replies

Resumable: the time range is split into sub-windows, each written to its own
file with a .done marker; re-running the SAME command skips completed windows
and only re-pulls the rest, then merges. Handy on flaky connections.

Config: see config.py / .env  (TURN_TOKEN, TURN_DISSEM_PATH).

Usage:
  python turn_export_anon.py --from 2026-06-04T00:00:00.000Z --auto-band
  python turn_export_anon.py --from ... --until ... --campaign-regex '^promo arm \\d'
  python turn_export_anon.py --from ... --until ... --inbound
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
from http.client import HTTPException
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import config

BASE    = config.BASE
ACCEPT  = config.ACCEPT
ARM_LABEL   = {"1": "WA Text", "2": "WA Text+Image", "3": "WA Bundle"}
VARIANT_RE  = re.compile(r"^((0528_)?var\d+|uncertain_vid)$", re.IGNORECASE)
INBOUND     = False  # when True, pull farmer-sent inbound TEXT replies instead of sends
# Rough DEFAULT arm split, used only to populate the (non-authoritative) arm
# column in the raw pull. The real split CHANGES EVERY WEEK and is re-derived
# authoritatively by fix_arms_and_verify.py after the pull — so this default
# does not need to be correct for a given week.
ARM_MAP     = {str(i): ("1" if i <= 5 else "2" if i <= 11 else "3") for i in range(1, 19)}


class RateLimiter:
    def __init__(self, limit=55, window=60.0):
        self.limit = limit; self.window = window
        self.times: deque = deque(); self.lock = threading.Lock()

    def acquire(self):
        while True:
            with self.lock:
                now = time.time()
                while self.times and self.times[0] <= now - self.window:
                    self.times.popleft()
                if len(self.times) < self.limit:
                    self.times.append(now); return
                wait = self.times[0] + self.window - now
            time.sleep(max(wait, 0.02))


def http(url: str, tok: str, limiter: RateLimiter, body: dict | None = None) -> dict:
    data = json.dumps(body).encode() if body else None
    hdrs = {"Authorization": f"Bearer {tok}", "Accept": ACCEPT}
    if data:
        hdrs["Content-Type"] = "application/json"
    for attempt in range(8):
        limiter.acquire()
        try:
            req = urllib.request.Request(url, data=data, headers=hdrs,
                                         method="POST" if data else "GET")
            with urllib.request.urlopen(req, timeout=120) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:200]
            if e.code == 429:
                rst = e.headers.get("x-ratelimit-reset")
                try:
                    wait = max(float(rst) - time.time() + 0.5, 1.0)
                except (TypeError, ValueError):
                    wait = 15.0
                print(f"  429 backoff {wait:.0f}s", flush=True)
                time.sleep(wait)
                continue
            if e.code >= 500:
                time.sleep(10 * (attempt + 1)); continue
            sys.exit(f"ERROR: HTTP {e.code} — {detail}")
        except (urllib.error.URLError, TimeoutError, ConnectionError,
                HTTPException, json.JSONDecodeError) as e:
            # transient network/server drops (incl. RemoteDisconnected) — retry
            time.sleep(10 * (attempt + 1)); continue
    sys.exit(f"ERROR: {url} failed after 8 retries")


def norm(elem: dict) -> list[dict]:
    return elem["messages"] if isinstance(elem, dict) and "messages" in elem else [elem]


def parse_iso(s: str) -> float:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
        tzinfo=dt.timezone.utc).timestamp()


def iso_ms(epoch: float) -> str:
    return dt.datetime.utcfromtimestamp(epoch).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _first_var_ts(frm: str, until: str, tok: str, limiter: RateLimiter,
                  ordering: str, max_pages: int) -> int | None:
    """Page [frm, until] in `ordering` and return the timestamp of the first
    var* outbound message encountered (earliest if asc, latest if desc), or
    None if none found within max_pages. Skips auto-replies/inbound."""
    body = {"from": frm, "until": until, "ordering": ordering, "page_size": 1000}
    cur = http(f"{BASE}/messages/cursor", tok, limiter, body)["cursor"]
    pages = 0
    while cur and pages < max_pages:
        resp = http(f"{BASE}/messages/cursor/{cur}", tok, limiter)
        pages += 1
        for elem in resp.get("data", []):
            for m in norm(elem):
                v1 = (m.get("_vnd") or {}).get("v1", {}) or {}
                if v1.get("direction") != "outbound":
                    continue
                camp = ((v1.get("author") or {}).get("name", "") or "").strip()
                if VARIANT_RE.match(camp):
                    try:
                        return int(m.get("timestamp"))
                    except (TypeError, ValueError):
                        return None
        cur = (resp.get("paging") or {}).get("next")
    return None


def detect_band(frm: str, until: str, tok: str, limiter: RateLimiter) -> tuple[float, float]:
    """Find the [start, end] epoch range that actually contains var* sends.
    Start: ascending scan for the first var message. End: binary search on the
    `from` boundary (var traffic is one contiguous blast, so 'is there still a
    var message at/after time T' is monotonic in T). Costs ~60 requests total,
    far cheaper than chunking the whole sparse range."""
    t0, t1 = parse_iso(frm), parse_iso(until)
    # scan deep enough to page past any leading promo traffic before the campaign
    start_ts = _first_var_ts(frm, until, tok, limiter, "asc", max_pages=400)
    if start_ts is None:
        print("  [auto-band] no var messages found; using full range", flush=True)
        return t0, t1
    # binary search for the last instant var traffic is still flowing
    lo, hi = float(start_ts), t1
    for _ in range(16):
        if hi - lo <= 60:
            break
        mid = (lo + hi) / 2
        if _first_var_ts(iso_ms(mid), until, tok, limiter, "asc", max_pages=3) is not None:
            lo = mid          # var traffic still present at/after mid
        else:
            hi = mid          # blast already over by mid
    end_ts = lo
    print(f"  [auto-band] detected send band: {iso_ms(start_ts)} .. {iso_ms(end_ts)} "
          f"({(end_ts - start_ts) / 3600:.1f}h)", flush=True)
    # pad both edges so fixed slicing can't clip the very first/last sends
    return start_ts - 300, end_ts + 300


def pull_window(idx: int, frm: str, until: str, tok: str, limiter: RateLimiter,
                phone_map: dict, resume_dir: Path, cols: list, progress: dict) -> int:
    """Pull one sub-window into its own file + .done marker. If the .done marker
    already exists (prior run finished this window), skip instantly. This makes
    the whole pull resumable: a killed run is restarted and only the unfinished
    windows re-run."""
    done = resume_dir / f"win_{idx:03d}.done"
    out = resume_dir / f"win_{idx:03d}.csv"
    if done.exists():
        progress[idx] = 0
        return -1  # skipped (already complete)
    tmp = resume_dir / f"win_{idx:03d}.tmp"
    cnt = 0; unm = 0
    with open(tmp, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        body = {"from": frm, "until": until, "ordering": "asc", "page_size": 1000}
        cursor = http(f"{BASE}/messages/cursor", tok, limiter, body)["cursor"]
        while cursor:
            resp = http(f"{BASE}/messages/cursor/{cursor}", tok, limiter)
            batch = []
            for elem in resp.get("data", []):
                for m in norm(elem):
                    v1 = (m.get("_vnd") or {}).get("v1", {}) or {}
                    if INBOUND:
                        # farmer-sent text replies only
                        if v1.get("direction") != "inbound" or m.get("type") != "text":
                            continue
                        ph = m.get("from", "")[-10:]   # farmer phone, key only
                        ppbno = phone_map.get(ph)
                        if not ppbno:
                            unm += 1
                            continue
                        ts = m.get("timestamp", "")
                        try:
                            ts = dt.datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
                        except (ValueError, TypeError):
                            pass
                        batch.append({
                            "ppbno": ppbno,
                            "reply_timestamp": ts,
                            "reply_text": (m.get("text") or {}).get("body", "") or "",
                            "in_reply_to": v1.get("in_reply_to") or "",
                        })
                        continue
                    if v1.get("direction") != "outbound":
                        continue
                    camp = ((v1.get("author") or {}).get("name", "") or "").strip()
                    if not VARIANT_RE.match(camp):
                        continue
                    ph = m.get("to", "")[-10:]   # lookup key only, never written
                    ppbno = phone_map.get(ph)
                    if not ppbno:
                        unm += 1
                        continue
                    vn_m = re.search(r"\d+", camp)
                    vn = vn_m.group() if vn_m else "?"
                    arm = ARM_MAP.get(vn, "?")
                    ts = m.get("timestamp", "")
                    try:
                        ts = dt.datetime.utcfromtimestamp(int(ts)).isoformat() + "Z"
                    except (ValueError, TypeError):
                        pass
                    # NOTE: deliberately NOT writing message_id — the WhatsApp
                    # wamid base64-encodes the recipient phone number, which
                    # would defeat the phone-never-on-disk rule.
                    batch.append({
                        "ppbno": ppbno, "variant": camp, "arm": arm,
                        "arm_label": ARM_LABEL.get(arm, ""),
                        "last_status": v1.get("last_status") or "no_status",
                        "timestamp": ts,  # send time
                        # when the message reached its current/furthest status
                        # (read -> read time, delivered -> delivered time, etc.)
                        "last_status_timestamp": v1.get("last_status_timestamp") or "",
                    })
            if batch:
                w.writerows(batch); cnt += len(batch)
            cursor = (resp.get("paging") or {}).get("next")
    os.replace(tmp, out)              # atomic: file only appears when complete
    done.write_text(f"{cnt} {unm}")
    progress[idx] = cnt
    return cnt


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="frm", required=True,
                    help="ISO timestamp e.g. 2026-06-04T00:00:00.000Z")
    ap.add_argument("--until", dest="until",
                    default=(dt.datetime.utcnow() - dt.timedelta(minutes=5))
                    .strftime("%Y-%m-%dT%H:%M:%S.000Z"))
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--chunks", type=int, default=0,
                    help="time sub-windows to split into (default workers*8). "
                         "Oversplitting load-balances when sends cluster in a band.")
    ap.add_argument("--auto-band", action="store_true",
                    help="probe for the actual send band, then slice only that "
                         "band into --slice-minutes chunks (avoids empty-region "
                         "waste and the idle-worker tail).")
    ap.add_argument("--slice-minutes", type=int, default=15,
                    help="with --auto-band, width of each sub-window in minutes.")
    ap.add_argument("--campaign-regex", default=None,
                    help="override the campaign-name filter (default: the var* "
                         "A/B variants). e.g. '^promo arm \\d' to pull the promo "
                         "campaigns instead. Case-insensitive.")
    ap.add_argument("--inbound", action="store_true",
                    help="pull farmer-sent INBOUND text replies (ppbno, "
                         "reply_timestamp, reply_text, in_reply_to) instead of "
                         "outbound sends. Ignores --auto-band/--campaign-regex.")
    ap.add_argument("--out-dir", default=str(config.DATA_DIR))
    args = ap.parse_args()

    tok = config.require_token()
    dissem_path = config.require_dissem()
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    # inbound mode: pull farmer-sent text replies instead of outbound sends
    global INBOUND
    INBOUND = args.inbound
    if INBOUND:
        print("INBOUND mode: pulling farmer-sent TEXT replies", flush=True)
    # optional: pull a different campaign (e.g. promo) instead of the var* variants
    if args.campaign_regex:
        global VARIANT_RE
        VARIANT_RE = re.compile(args.campaign_regex, re.IGNORECASE)
        print(f"campaign filter overridden -> {args.campaign_regex!r}", flush=True)

    # Phase 1: load phone->ppbno mapping in memory
    print("Loading phone->ppbno mapping...", flush=True)
    phone_map: dict[str, str] = {}
    with open(dissem_path, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            ph = row["MobileNo"].strip()[-10:]
            phone_map[ph] = row["ppbno"].strip()
    print(f"  {len(phone_map):,} mappings loaded", flush=True)

    # Phase 2: RESUMABLE pull. Split the band into sub-windows; each window is
    # pulled into its own file + .done marker. A killed run is simply re-run
    # with the same command: completed windows skip instantly, only unfinished
    # ones re-pull. Band/windows are computed once and saved so they're
    # identical across restarts (auto-band's binary search varies by seconds).
    limiter = RateLimiter(55)
    n = args.workers
    cols = (["ppbno", "reply_timestamp", "reply_text", "in_reply_to"] if INBOUND
            else ["ppbno", "variant", "arm", "arm_label", "last_status", "timestamp", "last_status_timestamp"])
    resume_dir = out_dir / f"_resume_{args.frm[:10]}"
    resume_dir.mkdir(parents=True, exist_ok=True)
    wjson = resume_dir / "windows.json"

    if wjson.exists():
        meta = json.loads(wjson.read_text())
        windows = [tuple(w) for w in meta["windows"]]
        t0lbl, t1lbl = meta["t0"], meta["t1"]
        ndone = sum(1 for i in range(len(windows)) if (resume_dir / f"win_{i:03d}.done").exists())
        print(f"RESUMING prior run: {ndone}/{len(windows)} windows already complete", flush=True)
    else:
        t0, t1 = parse_iso(args.frm), parse_iso(args.until)
        if args.auto_band and not INBOUND:
            print("Detecting send band...", flush=True)
            t0, t1 = detect_band(args.frm, args.until, tok, limiter)
            slice_s = max(args.slice_minutes, 1) * 60
            chunks = max(int((t1 - t0) / slice_s + 0.999), n)
        else:
            chunks = args.chunks if args.chunks > 0 else n * 8
        edges = [t0 + (t1 - t0) * i / chunks for i in range(chunks + 1)]
        windows = [(iso_ms(edges[i]), iso_ms(edges[i + 1] - 0.001)) for i in range(chunks)]
        t0lbl, t1lbl = iso_ms(t0), iso_ms(t1)
        wjson.write_text(json.dumps({"t0": t0lbl, "t1": t1lbl, "windows": windows}))

    print(f"Pulling messages: {t0lbl} -> {t1lbl}", flush=True)
    print(f"  {n} workers draining {len(windows)} sub-windows, rate<=55/min", flush=True)

    progress: dict = {}
    stop_mon = threading.Event()
    def monitor():
        while not stop_mon.wait(30):
            nd = sum(1 for i in range(len(windows)) if (resume_dir / f"win_{i:03d}.done").exists())
            try:
                rr = sum(progress.values())
            except RuntimeError:
                rr = 0
            print(f"  [progress] {nd}/{len(windows)} windows done, ~{rr:,} rows this run", flush=True)
    mon = threading.Thread(target=monitor, daemon=True)
    mon.start()
    try:
        with ThreadPoolExecutor(max_workers=n) as ex:
            futs = {ex.submit(pull_window, i, a, b, tok, limiter,
                              phone_map, resume_dir, cols, progress): i
                    for i, (a, b) in enumerate(windows)}
            for fut in as_completed(futs):
                i = futs[fut]; c = fut.result()
                print(f"  window {i} done: {'skip (cached)' if c < 0 else f'{c:,} rows'}", flush=True)
    finally:
        stop_mon.set()

    # Merge only when EVERY window is complete; else stop and let a re-run resume.
    missing = [i for i in range(len(windows)) if not (resume_dir / f"win_{i:03d}.done").exists()]
    if missing:
        print(f"\nINCOMPLETE: {len(missing)} window(s) still pending: {missing[:12]}"
              f"{'...' if len(missing) > 12 else ''}\nRe-run the same command to resume.", flush=True)
        sys.exit(2)

    stamp = dt.datetime.utcnow().strftime("%Y%m%d_%H%M")
    out_path = out_dir / f"farmers_delivery_status_anon_{stamp}.csv"
    counts: dict = {}; status_counts: dict = {}; total = 0; unmatched = 0
    with open(out_path, "w", newline="", encoding="utf-8-sig") as fout:
        writer = csv.DictWriter(fout, fieldnames=cols)
        writer.writeheader()
        for i in range(len(windows)):
            wf = resume_dir / f"win_{i:03d}.csv"
            if wf.exists():
                with open(wf, encoding="utf-8", newline="") as fin:
                    for row in csv.DictReader(fin, fieldnames=cols):
                        writer.writerow(row); total += 1
                        counts[row["variant"]] = counts.get(row["variant"], 0) + 1
                        status_counts[row["last_status"]] = status_counts.get(row["last_status"], 0) + 1
            try:
                unmatched += int((resume_dir / f"win_{i:03d}.done").read_text().split()[1])
            except (ValueError, IndexError):
                pass

    print(f"\nDone. {total:,} rows written -> {out_path}", flush=True)
    print(f"Status: {status_counts}", flush=True)
    print(f"Variants ({len(counts)}): {sorted(counts.keys())}", flush=True)
    print(f"Unmatched phone numbers: {unmatched}", flush=True)
    shutil.rmtree(resume_dir, ignore_errors=True)


if __name__ == "__main__":
    main()
