# redcap-condense

Condense a longitudinal (multi-event, multi-arm) REDCap CSV export into a
single wide file with **one row per record**.

REDCap's longitudinal exports store one row per *(record, event)*, so a
participant seen at baseline and follow-up spans several mostly-empty rows.
`redcap-condense` turns that into one row per record, with columns from each
event side by side.

## Example

Input (`export.csv`):

```csv
record_id,redcap_event_name,redcap_data_access_group,age,weight,moca,demographics_complete
0001,baseline_arm_1,site_a,70,80.5,,2
0001,followup_arm_1,site_a,,79.0,27,0
0002,baseline_arm_2,site_b,65,60.0,,2
0002,followup_arm_2,site_b,,61.2,25,0
```

`condense_redcap_csv export.csv --merge-arms` writes `export_condensed.csv`:

```csv
record_id,redcap_arm,redcap_data_access_group,age,weight__baseline,demographics_complete__baseline,weight__followup,moca,demographics_complete__followup
0001,1,site_a,70,80.5,2,79.0,27,0
0002,2,site_b,65,60.0,2,61.2,25,0
```

Without `--merge-arms`, each arm keeps its own columns
(`weight__baseline_arm_1`, `weight__baseline_arm_2`, ...).

## What it does

1. **Merge arms** (optional, `--merge-arms`): `baseline_arm_1` and
   `baseline_arm_2` both become `baseline`, and the arm number goes in a
   `redcap_arm` column.
2. **Record-level columns** such as `redcap_data_access_group` are taken once
   per record and placed right after the record ID.
3. **Split by event** using `redcap_event_name`.
4. **Drop empty columns** within each event.
5. **Drop empty events**: events with no values outside bookkeeping columns
   (`*_complete`, `*_timestamp`, `redcap_*`). Unchecked checkboxes (`0`)
   don't count as values.
6. **Suffix shared fields**: a field that survives in more than one event
   gets the event name appended (`weight__baseline`). Fields found in only
   one event keep their original name.
7. **Join** the events side by side on the record ID.

The record ID is read as text, so leading zeros (`0001`) are preserved, and
integer codes stay integers (`1`, not `1.0`) even when the join introduces
blanks.

## Installation

Requires Python 3.10+ and pandas 2.0+.

```bash
pip install git+https://github.com/SympatiCog/redcap-condense.git
# or, from a clone
uv sync
```

## Command line

```bash
condense_redcap_csv export.csv                        # -> export_condensed.csv
condense_redcap_csv export.csv wide.csv --merge-arms
condense_redcap_csv export.csv wide.csv --how inner   # only records in every event
python -m redcap_condense export.csv wide.csv
```

| Option | Default | Description |
| --- | --- | --- |
| `--record-id COL` | first column | Record ID column. |
| `--event-col COL` | `redcap_event_name` | Event column. |
| `--how {outer,inner}` | `outer` | `outer` keeps every record; `inner` keeps only records present in every kept event. |
| `--merge-arms` | off | Merge the same event across arms and add a `redcap_arm` column. |
| `--metadata-pattern REGEX` | `^redcap_\|_complete$\|_timestamp$` | Bookkeeping columns that don't count as data when deciding whether to keep an event. `''` counts everything. |
| `--record-cols COL[,COL...]` | `redcap_data_access_group` | Comma-separated record-level columns kept once per record. `''` for none. |
| `--sep SEP` | `__` | Separator between field and event name. |
| `--repeats {error,drop}` | `error` | Handling of repeating instrument/event rows. |
| `-v` / `-q` | | More or less logging. |

Exit codes: `0` success, `1` the export could not be condensed or parsed,
`2` file or argument problem.

## Python

```python
from redcap_condense import RedcapCondenser, condense_redcap, condense_redcap_csv

# From a file, optionally writing the result
wide = condense_redcap_csv("export.csv", "wide.csv", merge_arms=True)

# From a DataFrame already in memory
wide = condense_redcap(df, how="inner")

# Reusable configuration
condenser = RedcapCondenser(merge_arms=True, record_cols=["redcap_data_access_group", "site"])
wide = condenser.condense_csv("export.csv")
```

## Things to know

- **Export with raw variable names**, not labels. The tool needs the
  `redcap_event_name` column and unique event names such as `baseline_arm_1`.
  An export without an event column is treated as a classic (non-longitudinal)
  project.
- **Repeating instruments and events** would produce several rows per
  record, so they are rejected by default. Remove them from the report, or
  pass `--repeats drop` to discard them with a warning.
- **`--merge-arms`** needs event names ending in `_arm_N`. It stops with an
  error if a record is enrolled in more than one arm.
- **Column names depend on the data.** A field is suffixed only if it
  appears in more than one kept event, so a later export with new data
  can rename columns (`moca` becomes `moca__followup` once baseline values
  appear). Pin column names in downstream code with care.
- Rows with a blank event name are dropped with a warning. Name collisions
  caused by suffixing raise an error; choose a different `--sep`.
- **Protected health information.** REDCap exports often contain PHI. The
  included `.gitignore` excludes `*.csv` so exports aren't committed by
  accident.

## Development

```bash
uv sync
uv run pytest
```

## License

[MIT](LICENSE)
