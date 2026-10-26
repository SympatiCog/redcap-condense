# CLAUDE.md

Guidance for Claude Code when working in this repository.

## Project

`redcap-condense` reshapes longitudinal REDCap CSV exports (one row per
record × event) into one row per record. It's a single-module package:

- `redcap_condense.py`: library, functional API and CLI (`main`).
- `test_redcap_condense.py`: pytest suite, with inline CSV fixtures.
- Console script `condense_redcap_csv` → `redcap_condense:main`.

## Commands

```bash
uv sync                 # install with the dev group (pytest)
uv run pytest -q        # run tests
uv run condense_redcap_csv export.csv out.csv -v
```

## Architecture

`RedcapCondenser` (a dataclass) holds all options; `condense()` is the
pipeline, in order:

1. `_resolve_key`: record ID column, which defaults to the first column.
2. `_handle_repeats`: repeating rows raise an error or are dropped.
3. If there's no event column, the export is treated as a classic project.
4. `_drop_missing_events` → `_merge_arms` (optional) → `_split_record_cols`
5. `split_events` → `_filter_events` (`has_data`) → `_disambiguate`
6. `_check_column_names` → `pd.concat(axis="columns", join=how)` → left-join
   the record-level columns.

`condense_redcap` / `condense_redcap_csv` are thin wrappers that pass
`**options` to `RedcapCondenser`.

## Invariants: keep these when changing code

- Output is **one row per record**. Events are aligned by record ID index,
  never by row position. Duplicate IDs within an event raise an error.
- Record-level columns are left-joined, so they never add or remove records,
  including with `how="inner"`.
- The record ID is read as `string` (leading zeros). CSVs are read with
  `dtype_backend="numpy_nullable"` so integer codes stay integers after the
  outer join.
- Bookkeeping columns (`metadata_pattern`) don't decide whether an event is
  kept, but they stay in the output for kept events. Checkbox columns
  (`___`) with value `0` don't count as data.
- Problems with the data raise `RedcapCondenseError` (a `ValueError`).
  Lossy-but-recoverable situations (dropped repeats or blank-event rows,
  conflicting record-level values, empty result) log a warning instead.
- Every new option is added to the dataclass, its docstring, the CLI parser,
  `main()`, and the README options table.

## Conventions

- Python ≥ 3.10, pandas ≥ 2.0; no other runtime dependencies.
- Use `log` (module logger) for messages, not `print`. The CLI configures
  logging; the library doesn't.
- Tests build small inline CSVs or DataFrames; add one for each behavior
  change. Never commit real REDCap exports. `*.csv` is gitignored because
  exports can contain PHI.
