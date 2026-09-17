# WhatsApp Delivery-Status Export & Analysis (Turn.io)

Tooling to pull WhatsApp message delivery/read status from the [Turn.io](https://whatsapp.turn.io) Data Export API for a large weekly A/B advisory campaign, and to turn those pulls into anonymised, analysis-ready datasets and reports.

The campaign sends weekly weather advisories to a large farmer base (~1.4M) split across treatment arms (WA Text, WA Text+Image, WA Bundle) and many message variants. Each week we pull every message's delivery status, reconcile it against the forecast plan, and produce delivery/read breakdowns by district, mandal, and cluster.

## Privacy model (read this first)

- **Phone numbers are never written to disk.** The puller uses the phone number only as an in-memory key to look up each farmer's passbook number (`ppbno`) in the dissemination roster, then writes `ppbno` only. Every output is keyed by `ppbno`.
- **`message_id` is deliberately not written.** A WhatsApp `wamid` base64-encodes the recipient phone number, so emitting it would defeat the phone-free guarantee.
- **The dissemination roster and all data outputs stay out of the repo.** They contain phone numbers (roster) or are large/regenerated (outputs). See `.gitignore`.
- Inbound farmer replies (`--inbound`) contain free text the farmer wrote; that content cannot be anonymised (sentiment needs the words). Handle those files accordingly.
- **`--phone-key` is the deliberate exception to all of the above.** It skips the roster and writes the recipient's phone number as the key column (`phone`) instead of `ppbno`, for an operator who has no roster and needs numbers beside the status. Everything it writes is PII: keep it out of the repo and shared drives, and delete it after handover. `build_week_matrix.py` accepts phone-keyed pulls as-is.

## Repository layout

```
config.py                    central config: loads .env, exposes paths/token (no machine paths)
turn_export_anon.py          the puller — pulls a campaign's messages -> anonymised CSV (resumable)
turn_export_with_phone.py    the SAME puller with --phone-key forced: no roster, phone-keyed, NOT anonymised
fix_arms_and_verify.py       recompute arm column + verify per-variant counts vs the forecast
build_geo_xlsx.py            8-tab delivery report by district / mandal / cluster
build_status_geo_file.py     ppbno + status + last_status_timestamp + geography (shareable CSV)
build_week_matrix.py         merge N weekly pulls into ONE wide row per farmer (post-processing, not a puller)
test_week_matrix.py          offline smoke test for build_week_matrix.py (fake pulls + fake roster)
build_advisory_report.py     fill the dept's per-variant advisory template (with forecast text)
wow_read_change.py           week-over-week read-rate change by geography
sample_farmers_per_mandal.py random N-farmers-per-mandal sample (district, mandal, ppbno)
reference/                   optional local reference workbooks (gitignored; see reference/README.md)
data/                        pull outputs + resumable working dirs (gitignored)
.env.example                 copy to .env and fill in
requirements.txt             openpyxl (reports); the puller itself is stdlib-only
```

## Setup

1. Python 3.9+.
2. `pip install -r requirements.txt`
3. `cp .env.example .env` and fill in:
   - `TURN_TOKEN` — your Turn.io Data Export API bearer token.
   - `TURN_DISSEM_PATH` — absolute path to your dissemination roster CSV (kept outside the repo). Required columns: `MobileNo, ppbno, district_name, mandal_name, cluster_name, village_name`.
4. Optional: drop reference workbooks into `reference/` (see `reference/README.md`) for canonical geography labels and the advisory-report template/forecast text.

## The Turn.io Data Export API (what the puller relies on)

- **Endpoint:** `POST /v1/data/messages/cursor` (with `{from, until, ordering, page_size}`) returns a cursor; `GET /v1/data/messages/cursor/{cursor}` returns a page plus the next cursor. Auth is `Authorization: Bearer <token>`.
- **Rate limit:** per number, reported in the `x-ratelimit-limit` header (60/min on our number as of 2026-09-17). The puller starts at 55/min and re-paces itself to 92% of whatever Turn reports, printing the detected value; `--rate-limit N` pins it manually. `page_size` is capped at 1000. `until` cannot be in the future.
- **Practical ceiling:** ~55K messages/min (55 req/min x 1000). Pulling the full ~1.4M base takes ~25-30 min under good conditions.
- **Message shape:** outbound campaign name is in `_vnd.v1.author.name`; direction in `_vnd.v1.direction`; furthest status in `_vnd.v1.last_status`; the time that status was reached in `_vnd.v1.last_status_timestamp`; send time in the top-level `timestamp`. Inbound messages are wrapped as `{messages:[...], contacts:[...]}`; text is in `text.body`.
- **Media:** inbound images/voice notes are retrievable via `GET /v1/media/{media-id}`, but this toolkit pulls text only and does not download media (PII).

## The puller: `turn_export_anon.py`

Pulls one campaign's messages over a time window, joins phone -> ppbno in memory, and writes an anonymised CSV to `data/`.

```bash
# A/B variant sends for a campaign, auto-detecting the send window:
python turn_export_anon.py --from 2026-06-04T00:00:00.000Z --auto-band

# bound to a single send day (needed when weeks reuse the same varN names — see gotchas):
python turn_export_anon.py --from 2026-06-05T00:00:00.000Z --until 2026-06-06T00:00:00.000Z --auto-band

# a different campaign by name (e.g. the launch promo):
python turn_export_anon.py --from ... --until ... --campaign-regex '^promo arm \d'

# farmer-sent inbound TEXT replies:
python turn_export_anon.py --from 2026-05-29T00:00:00.000Z --inbound --chunks 480
```

Key flags:

- `--from` (required) / `--until` (default: now minus 5 min). ISO 8601, e.g. `2026-06-04T00:00:00.000Z`.
- `--auto-band` — probe for the actual send band and slice only that, avoiding empty-region waste. Do not use for `--inbound` (replies are scattered, not banded).
- `--chunks N` — split the window into N sub-windows (default `workers*8`). More chunks = finer resume granularity.
- `--slice-minutes M` — with `--auto-band`, width of each sub-window (default 15).
- `--campaign-regex RE` — override the campaign-name filter (default matches `var1..varN` / `MMDD_varN` such as `0528_var7` / `uncertain_vid`).
- `--inbound` — pull farmer-sent text replies instead of sends.
- `--phone-key` — no roster needed; the key column is `phone` (recipient number) instead of `ppbno`, and the output is named `farmers_delivery_status_with_phone_<stamp>.csv`. `turn_export_with_phone.py` is the same thing with the flag forced on. The `build_*`/`wow_*` report scripts expect ppbno-keyed `_anon_` files and ignore these; only `build_week_matrix.py` accepts them. See the privacy model above.
- `--rate-limit N` — pin the pace at N requests/min instead of adapting to Turn's header.
- `--workers N` (default 5), `--out-dir` (default `data/`).

**Resumability.** The window is split into sub-windows; each is pulled into its own file with a `.done` marker, and only when all are done are they merged into the final `farmers_delivery_status_anon_<UTCstamp>.csv`. If the run dies (flaky connection, machine sleep), re-run the **exact same command**: completed sub-windows are skipped and only the rest re-pull. Transient network drops (`RemoteDisconnected`, resets, timeouts) are retried with backoff (8 attempts) before a window gives up.

Output columns:

| Mode | Columns |
|------|---------|
| default / `--campaign-regex` | `ppbno, variant, arm, arm_label, last_status, timestamp, last_status_timestamp` (`phone` replaces `ppbno` with `--phone-key`) |
| `--inbound` | `ppbno, reply_timestamp, reply_text, in_reply_to` |

`arm`/`arm_label` in the raw pull come from a rough default split and are **not** authoritative — `fix_arms_and_verify.py` recomputes them.

## The analysis scripts

All read the most recent (or a specified) pull from `data/` and write back to `data/`.

### `fix_arms_and_verify.py`
Recomputes `arm`/`arm_label` from the variant number (authoritative) and prints a verification table: per-variant pulled-vs-forecast counts, opt-out tolerance check, and per-arm failed% / reached% / read%(of reached).
```bash
python fix_arms_and_verify.py [optional_path_to_pull.csv]
```
**Edit per campaign:** `arm_for()` (the arm split), `vnum()` (any renamed variant, e.g. a video variant not named `varN`), and the `EXPECTED` dict (forecast counts). These change every week.

### `build_geo_xlsx.py`
Builds `delivery_status_by_geography.xlsx` — an 8-tab workbook: a dept-facing "DeliveredFailed" view (`Total Sent / Delivered / Failed / %`) and a raw "Full Breakdown" view (`Sent / Delivered / Read / Failed / %`), each by district, mandal, and cluster. `Delivered = Total - Failed`.
```bash
python build_geo_xlsx.py
```

### `build_status_geo_file.py`
Builds a single shareable, phone-free CSV: `ppbno, last_status, last_status_timestamp, district, mandal, cluster`.
```bash
python build_status_geo_file.py [pull.csv] [out.csv]
```

### `build_advisory_report.py`
Fills the department's per-variant report template: one tab per week, one row per variant, with the districts/mandals that variant covered, the variant's forecast text, and `Total Sent / Delivered / Failed / %`.
```bash
python build_advisory_report.py
```
**Edit per run:** the `WEEKS` list (tab name, forecast week label, that week's pull CSV). Needs `reference/advisory_report_template.xlsx` and `reference/forecast_inputs.xlsx`.

### `build_week_matrix.py`
Merges N weekly pulls into a single wide CSV, one row per farmer, with a block of columns per week: `{w}_variant, {w}_arm, {w}_status, {w}_read, {w}_status_ts`. Weeks a farmer was not pulled in show `not_in_pull`. A farmer with two messages in one week collapses to the furthest status reached. Runs through a temporary SQLite file, so ~1.4M farmers is fine.

This is post-processing on files `turn_export_anon.py` already wrote. It is not a second pull method and never calls the API.
```bash
python build_week_matrix.py     --week w1=data/farmers_delivery_status_anon_<w1>.csv     --week w2=data/farmers_delivery_status_anon_<w2>.csv     --week w3=data/farmers_delivery_status_anon_<w3>.csv     --week w4=data/farmers_delivery_status_anon_<w4>.csv     --geo
```
By default it writes a 200-row `*.preview.csv`, prints the per-week status mix and variant counts, and stops. Check the preview, then re-run with `--approved` to write the full file. Other flags: `--all-roster` (every roster ppbno gets a row, even if never messaged), `--limit-rows N`, `--keep-db`.

**`--with-phone`** joins `MobileNo` from the roster in as a `phone` column. This is the one place in the toolkit that writes a phone number to disk. It exists for an operator who already holds the roster and needs the number beside the status. Treat the output as PII: keep it out of the repo and shared drives, and delete it when the job is done.

Run `fix_arms_and_verify.py` on each weekly pull first; the `arm` column straight out of the puller is not authoritative.
```bash
python test_week_matrix.py     # offline smoke test, no token or roster needed
```

### `wow_read_change.py`
Week-over-week read-rate (`read / (read + delivered)`) change by district / mandal / cluster, with a summary tab. Compare pulls at **matched maturity** (equal days-after-send) for the numbers to be valid.
```bash
python wow_read_change.py
```
**Edit per run:** the `WEEKS` list (label, pull CSV per week).

### `sample_farmers_per_mandal.py`
Random sample of N farmers per mandal (`district, mandal, ppbno`), reproducible via a fixed seed.
```bash
python sample_farmers_per_mandal.py 100        # 100 per mandal, seed 42
```

## Typical weekly workflow

1. **Ask the program lead for this week's design:** number of variants, the arm split (which variant numbers map to Text / Text+Image / Bundle), and the forecast/expected-sends table. This changes every week.
2. **Pull the campaign:** `python turn_export_anon.py --from <day before send> --until <day after send> --auto-band`. Bound with `--until` to the single send day.
3. **Verify:** update `arm_for`/`vnum`/`EXPECTED` in `fix_arms_and_verify.py`, then run it. Expect the pulled total to be ~0.1-0.2% under the forecast (opt-outs), spread proportionally across variants.
4. **Report:** run `build_geo_xlsx.py` (geographic delivery) and, if the dept wants it, `build_advisory_report.py`.
5. **Merge weeks:** once several weeks are pulled, `build_week_matrix.py` gives one wide row per farmer across all of them (add `--with-phone` only if the operator needs numbers on disk).
6. **Optional trend:** once two or more weeks are pulled at matched maturity, run `wow_read_change.py`.

## Key concepts and gotchas

- **`last_status` is the furthest state reached** (read > delivered > sent, or failed). `last_status_timestamp` is when that state occurred, and it is the actual historical event time, not the pull time. That means you can re-pull an old campaign now and still get true read times.
- **Read rate matures over days.** Read counts keep climbing for a week+ after send. Only compare read rates across weeks at matched days-after-send.
- **Delivery is a recipient property, not a content one.** The ~29% failed rate is very stable week to week and near-uniform across arms/districts — it reflects dead/invalid/inactive WhatsApp numbers, not the message.
- **The design changes every week.** Variant count, arm split, and even variant naming vary. Never assume last week's split. Update `fix_arms_and_verify.py` (and the report `WEEKS` lists) each week.
- **`varN` names are reused every week.** To pull one week's campaign, bound it with `--until` to its send day, or `--auto-band` will chain consecutive weeks together (they all match `varN`). Week 1 used a `0528_varN` prefix; later weeks are bare `varN`; occasionally a variant is renamed (e.g. `uncertain_vid` = variant 12 in one week) — handle that in `vnum()`.
- **Auto-band can be defeated by a large promo block** sent before the real campaign (its variant scan gives up). When that happens, use an explicit `--from`/`--until` window; the campaign-name filter still excludes the promo.
- **Inbound is expensive.** Replies are ~0.1% of traffic, so `--inbound` over the whole program still has to page through millions of messages to extract a few tens of thousands of replies. Use many `--chunks` and expect resume cycles.
- **`in_reply_to` is effectively empty for text replies** in this data, so there is no reliable per-message reply link. Attribute replies to a week by timing (reply time vs the preceding send wave) using the weekly pulls.

## Output data dictionary

| Column | Meaning |
|--------|---------|
| `ppbno` | Farmer passbook number (unique per farmer; the join/identity key) |
| `variant` | Campaign name as sent (e.g. `var5`, `0528_var5`, `uncertain_vid`, `Promo arm 1 ...`) |
| `arm` / `arm_label` | Treatment arm 1/2/3 = WA Text / WA Text+Image / WA Bundle (authoritative only after `fix_arms_and_verify.py`) |
| `last_status` | `read` \| `delivered` \| `sent` \| `failed` \| `no_status` (furthest state reached) |
| `timestamp` | Send time (UTC, ISO 8601) |
| `last_status_timestamp` | When `last_status` was reached (UTC); for `read` this is the read time |
| `reply_timestamp` / `reply_text` / `in_reply_to` | (inbound mode) reply time, reply text, and quoted-message id if any |

## Notes

- All times are UTC.
- "Delivered" in the dept-facing report equals `Total - Failed` (i.e. everything not failed, including `sent`/`no_status`), matching the department's two-bucket convention. The "Full Breakdown" tabs give the raw `sent/delivered/read/failed`.
- Nothing here downloads inbound media; only text is captured.
