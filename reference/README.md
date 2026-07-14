# reference/ — optional local reference files (NOT committed)

The report scripts read these workbooks if present. They contain
program-specific content (labels, templates, forecast text), so they live here
locally and are gitignored. Drop your copies in with these exact names.

### `geography_labels.xlsx` (optional)
Canonical Title-Case geography names. Any workbook with `DF By District`,
`DF By Mandal`, and `DF By Cluster` sheets whose leading columns are the
district / mandal / cluster names (row 1 is a header; a trailing `TOTAL` row is
ignored). Used to render nice labels and to preserve a stable row order in the
geographic report. If absent, scripts fall back to Python `.title()` on the
roster's lowercase names.

### `advisory_report_template.xlsx`
The department's blank per-variant report template — one tab per weekly
campaign. Used by `build_advisory_report.py`, which fills rows beneath each
tab's header row. Set the exact tab names in that script's `WEEKS` list.

### `forecast_inputs.xlsx`
Per-variant forecast text. `build_advisory_report.py` reads the `WAT` sheet,
which must have columns: `week`, `arm`, `message`, `unique_variant_tag`. The
`unique_variant_tag` is the variant number; `message` is the advisory text
(which contains the suggested rain dates).
