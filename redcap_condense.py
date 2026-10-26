"""Condense a longitudinal (multi-event / multi-arm) REDCap report export.

REDCap's longitudinal CSV export stores one row per (record, event). This
module reshapes it to one row per record:

1. Optionally merge arms: ``baseline_arm_1`` and ``baseline_arm_2`` both
   become ``baseline``, and the arm number goes in a ``redcap_arm`` column.
2. Pull record-level columns (e.g. ``redcap_data_access_group``) out once.
3. Split the remaining columns by ``redcap_event_name``.
4. Drop columns that are entirely empty within each event.
5. Drop events with no values outside bookkeeping columns (form status,
   survey timestamps, ``redcap_*`` fields). Unchecked checkboxes (``0``)
   don't count as values.
6. Suffix any field that survives in more than one event with the event name.
7. Join the events side by side on the record ID, with record-level
   columns placed right after it.

Python use::

    from redcap_condense import condense_redcap_csv
    df = condense_redcap_csv("export.csv", "condensed.csv")

CLI use::

    condense_redcap_csv export.csv condensed.csv
    python -m redcap_condense export.csv condensed.csv --how inner
    condense_redcap_csv export.csv --merge-arms
"""

from __future__ import annotations

import argparse
import logging
import re
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Sequence

import pandas as pd

__all__ = [
    "RedcapCondenser",
    "RedcapCondenseError",
    "condense_redcap",
    "condense_redcap_csv",
    "main",
]

log = logging.getLogger(__name__)

EVENT_COL = "redcap_event_name"
REPEAT_COLS = ("redcap_repeat_instrument", "redcap_repeat_instance")
RECORD_COLS = ("redcap_data_access_group",)
ARM_COL = "redcap_arm"
ARM_PATTERN = r"^(?P<event>.+)_arm_(?P<arm>\d+)$"
# REDCap exports checkbox options as ``field___code`` with 0 for unchecked.
CHECKBOX_PATTERN = r"___"
# Columns that can be filled even when no one entered data in an event.
METADATA_PATTERN = r"^redcap_|_complete$|_timestamp$"

JoinHow = Literal["outer", "inner"]
RepeatPolicy = Literal["error", "drop"]


class RedcapCondenseError(ValueError):
    """Raised when an export cannot be condensed to one row per record."""


def drop_empty_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``df`` without columns that are entirely missing."""
    return df.dropna(axis="columns", how="all")


@dataclass
class RedcapCondenser:
    """Configurable converter from a long REDCap export to a wide table.

    Parameters
    ----------
    record_id
        Record ID column. ``None`` uses the first column, which is where
        REDCap always places the record ID.
    event_col
        Column holding the unique event name.
    how
        ``"outer"`` keeps every record (missing events become NA).
        ``"inner"`` keeps only records present in every retained event.
    metadata_pattern
        Regex for bookkeeping columns. An event is kept only if some column
        not matching this pattern has a value. Bookkeeping columns of kept
        events stay in the output. ``None`` or ``""`` treats every column
        as data.
    record_cols
        Columns that describe the record rather than the event. Each is
        taken once per record (first non-missing value) and placed after
        the record ID. Names not in the export are ignored.
    sep
        Separator between a field name and its event name when a field
        appears in more than one event, e.g. ``weight__baseline_arm_1``.
    repeats
        What to do with rows from repeating instruments or events, which
        would otherwise produce several rows per record. ``"error"`` stops,
        ``"drop"`` discards them with a warning.
    merge_arms
        Treat the same event in different arms as one event, so
        ``weight__baseline`` replaces ``weight__baseline_arm_1`` and
        ``weight__baseline_arm_2``. The arm number is added as a
        record-level ``redcap_arm`` column. Records enrolled in more than
        one arm raise an error.
    """

    record_id: str | None = None
    event_col: str = EVENT_COL
    how: JoinHow = "outer"
    metadata_pattern: str | None = METADATA_PATTERN
    record_cols: Sequence[str] = RECORD_COLS
    sep: str = "__"
    repeats: RepeatPolicy = "error"
    merge_arms: bool = False

    def __post_init__(self) -> None:
        if self.how not in ("outer", "inner"):
            raise ValueError(f"how must be 'outer' or 'inner', got {self.how!r}")
        if self.repeats not in ("error", "drop"):
            raise ValueError(f"repeats must be 'error' or 'drop', got {self.repeats!r}")
        if self.metadata_pattern:
            re.compile(self.metadata_pattern)  # fail early on a bad regex
        self.record_cols = tuple(self.record_cols)

    # ---------------------------------------------------------------- I/O

    def read_csv(self, path: str | Path) -> pd.DataFrame:
        """Read a REDCap CSV export.

        The record ID is read as a string so IDs like ``0012`` keep their
        leading zeros. Nullable dtypes keep integer codes as integers after
        the outer join introduces missing values (``1``, not ``1.0``).
        """
        key = self.record_id or pd.read_csv(path, nrows=0).columns[0]
        return pd.read_csv(
            path,
            dtype={key: "string"},
            dtype_backend="numpy_nullable",
            low_memory=False,
        )

    def condense_csv(
        self, input_path: str | Path, output_path: str | Path | None = None
    ) -> pd.DataFrame:
        """Read, condense, and optionally write a REDCap export."""
        wide = self.condense(self.read_csv(input_path))
        if output_path is not None:
            Path(output_path).parent.mkdir(parents=True, exist_ok=True)
            wide.to_csv(output_path, index=False)
            log.info("Wrote %s", output_path)
        return wide

    # ----------------------------------------------------------- pipeline

    def condense(self, df: pd.DataFrame) -> pd.DataFrame:
        """Condense a long REDCap export DataFrame to one row per record."""
        key = self._resolve_key(df)
        df = self._handle_repeats(df)

        if self.event_col not in df.columns:
            log.info("No %r column; treating as a classic project.", self.event_col)
            self._check_unique(
                df.set_index(key), "export",
                hint=f" If this is a longitudinal export, re-export with raw "
                     f"variable names (not labels) so it has a {self.event_col!r} "
                     "column, or pass event_col (CLI: --event-col).",
            )
            return drop_empty_columns(df)

        df = self._drop_missing_events(df)
        if self.merge_arms:
            df = self._merge_arms(df, key)
        record_level, df = self._split_record_cols(df, key)
        events = self.split_events(df, key)
        events = self._filter_events(events)
        events = self._disambiguate(events)
        self._check_column_names(record_level, events)

        wide = pd.concat(events.values(), axis="columns", join=self.how)
        wide.index.name = key
        if not record_level.empty:
            # Left join so record-level columns never add or remove records.
            wide = record_level.reindex(wide.index).join(wide)
        if wide.empty:
            log.warning(
                "No records in the output. With how='inner', a record must "
                "appear in every kept event%s.",
                "" if self.merge_arms else "; in multi-arm projects consider "
                "merge_arms (CLI: --merge-arms)",
            )
        log.info(
            "Condensed %d rows into %d records x %d columns from %d events.",
            len(df), len(wide), wide.shape[1] + 1, len(events),
        )
        return wide.reset_index()

    def split_events(
        self, df: pd.DataFrame, key: str | None = None
    ) -> dict[str, pd.DataFrame]:
        """Split by event, index by record ID, and drop empty columns.

        Events keep the order in which they first appear in the export.
        """
        key = key or self._resolve_key(df)
        events: dict[str, pd.DataFrame] = {}
        for event, group in df.groupby(self.event_col, sort=False):
            frame = drop_empty_columns(
                group.drop(columns=self.event_col).set_index(key)
            )
            self._check_unique(frame, f"event {event!r}")
            events[str(event)] = frame
        return events

    # ------------------------------------------------------------ helpers

    def _resolve_key(self, df: pd.DataFrame) -> str:
        key = self.record_id or str(df.columns[0])
        if key not in df.columns:
            raise RedcapCondenseError(
                f"Record ID column {key!r} not found. "
                f"First columns are: {list(df.columns[:5])}"
            )
        return key

    def _handle_repeats(self, df: pd.DataFrame) -> pd.DataFrame:
        present = [c for c in REPEAT_COLS if c in df.columns]
        if not present:
            return df
        is_repeat = df[present].notna().any(axis="columns")
        n_repeat = int(is_repeat.sum())
        if n_repeat and self.repeats == "error":
            raise RedcapCondenseError(
                f"{n_repeat} rows come from repeating instruments or events, "
                "which would give several rows per record. Remove them from "
                "the report, or pass repeats='drop' (CLI: --repeats drop)."
            )
        if n_repeat:
            log.warning("Dropping %d repeating-instrument/event rows.", n_repeat)
        return df.loc[~is_repeat].drop(columns=present)

    def _drop_missing_events(self, df: pd.DataFrame) -> pd.DataFrame:
        missing = df[self.event_col].isna()
        if missing.any():
            log.warning("Dropping %d rows with no %s.", int(missing.sum()), self.event_col)
        return df.loc[~missing]

    def _merge_arms(self, df: pd.DataFrame, key: str) -> pd.DataFrame:
        """Strip ``_arm_N`` from event names and record the arm per record."""
        parts = df[self.event_col].astype("string").str.extract(ARM_PATTERN)
        unmatched = df.loc[parts["arm"].isna(), self.event_col].unique()
        if len(unmatched):
            raise RedcapCondenseError(
                f"Cannot merge arms: event names without an _arm_N suffix: "
                f"{list(unmatched[:5])}"
            )
        arms = parts["arm"].astype("Int64")
        n_arms = arms.groupby(df[key]).nunique()
        multi = n_arms.index[n_arms > 1]
        if len(multi):
            raise RedcapCondenseError(
                f"Cannot merge arms: records in more than one arm: {list(multi[:5])}"
            )
        if ARM_COL in df.columns:
            raise RedcapCondenseError(f"Cannot merge arms: export already has {ARM_COL!r}.")
        df = df.assign(**{self.event_col: parts["event"]})
        df.insert(1, ARM_COL, arms)
        return df

    def _split_record_cols(
        self, df: pd.DataFrame, key: str
    ) -> tuple[pd.DataFrame, pd.DataFrame]:
        """Separate record-level columns, one value per record."""
        names = (ARM_COL, *self.record_cols) if self.merge_arms else self.record_cols
        cols = [c for c in names if c in df.columns]
        if not cols:
            return pd.DataFrame(), df
        grouped = df.groupby(key, sort=False)[cols]
        n_unique = grouped.nunique()
        conflicts = [c for c in cols if (n_unique[c] > 1).any()]
        if conflicts:
            log.warning("Records with differing values across events in %s; "
                        "keeping the first.", conflicts)
        return drop_empty_columns(grouped.first()), df.drop(columns=cols)

    def is_metadata(self, column: str) -> bool:
        """True if ``column`` is bookkeeping rather than collected data."""
        return bool(self.metadata_pattern) and re.search(self.metadata_pattern, column) is not None

    def has_data(self, frame: pd.DataFrame) -> bool:
        """True if any non-bookkeeping column of ``frame`` holds a value.

        Checkbox columns count only when a box is checked, because REDCap
        exports unchecked boxes as ``0`` even where no one entered data.
        """
        for col in frame.columns:
            if self.is_metadata(str(col)):
                continue
            values = frame[col].dropna()
            if re.search(CHECKBOX_PATTERN, str(col)):
                values = values[pd.to_numeric(values, errors="coerce") != 0]
            if len(values):
                return True
        return False

    def _filter_events(
        self, events: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        kept = {e: f for e, f in events.items() if self.has_data(f)}
        dropped = [e for e in events if e not in kept]
        if dropped:
            log.info("Dropped events with no data outside bookkeeping columns: %s", dropped)
        if not kept:
            raise RedcapCondenseError("No event has data outside bookkeeping columns.")
        return kept

    def _disambiguate(
        self, events: dict[str, pd.DataFrame]
    ) -> dict[str, pd.DataFrame]:
        """Suffix fields that occur in more than one event with the event name."""
        counts = Counter(col for frame in events.values() for col in frame.columns)
        shared = {col for col, n in counts.items() if n > 1}
        if not shared:
            return events
        log.info("Suffixing %d fields found in multiple events.", len(shared))
        return {
            event: frame.rename(
                columns={c: f"{c}{self.sep}{event}" for c in shared & set(frame.columns)}
            )
            for event, frame in events.items()
        }

    @staticmethod
    def _check_column_names(
        record_level: pd.DataFrame, events: dict[str, pd.DataFrame]
    ) -> None:
        """Fail if suffixing produced a name that is already taken."""
        names = [*record_level.columns]
        for frame in events.values():
            names.extend(frame.columns)
        dupes = sorted({str(c) for c, n in Counter(names).items() if n > 1})
        if dupes:
            raise RedcapCondenseError(
                f"Duplicate output column names {dupes[:5]}: a suffixed field "
                "collides with an existing field. Pass a different sep "
                "(CLI: --sep)."
            )

    @staticmethod
    def _check_unique(frame: pd.DataFrame, where: str, hint: str = "") -> None:
        dupes = frame.index[frame.index.duplicated()].unique()
        if len(dupes):
            raise RedcapCondenseError(
                f"Duplicate record IDs in {where}: {list(dupes[:5])}.{hint}"
            )


# ------------------------------------------------------- functional API


def condense_redcap(df: pd.DataFrame, **options) -> pd.DataFrame:
    """Condense an in-memory REDCap export. Options match ``RedcapCondenser``."""
    return RedcapCondenser(**options).condense(df)


def condense_redcap_csv(
    input_path: str | Path, output_path: str | Path | None = None, **options
) -> pd.DataFrame:
    """Read a REDCap CSV export, condense it, and optionally write the result.

    Options match ``RedcapCondenser``. Returns the condensed DataFrame.
    """
    return RedcapCondenser(**options).condense_csv(input_path, output_path)


# ------------------------------------------------------------------ CLI


def _comma_list(value: str) -> list[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="condense_redcap_csv",
        description="Condense a longitudinal REDCap CSV export to one row per record.",
    )
    p.add_argument("input", type=Path, help="REDCap CSV report export")
    p.add_argument(
        "output", type=Path, nargs="?",
        help="output CSV (default: <input>_condensed.csv)",
    )
    p.add_argument("--record-id", help="record ID column (default: first column)")
    p.add_argument("--event-col", default=EVENT_COL, help="event column name")
    p.add_argument(
        "--how", choices=("outer", "inner"), default="outer",
        help="outer keeps all records; inner keeps records present in every event",
    )
    p.add_argument(
        "--metadata-pattern", default=METADATA_PATTERN,
        help="regex for bookkeeping columns that don't count as data when "
             "deciding whether to keep an event; '' counts everything "
             f"(default: {METADATA_PATTERN!r})",
    )
    p.add_argument(
        "--record-cols", type=_comma_list, default=list(RECORD_COLS),
        metavar="COL[,COL...]",
        help="comma-separated record-level columns kept once per record; "
             f"'' for none (default: {','.join(RECORD_COLS)})",
    )
    p.add_argument("--sep", default="__", help="field/event separator (default: __)")
    p.add_argument(
        "--repeats", choices=("error", "drop"), default="error",
        help="how to handle repeating instrument/event rows (default: error)",
    )
    p.add_argument(
        "--merge-arms", action="store_true",
        help="merge the same event across arms (baseline_arm_1 and "
             f"baseline_arm_2 -> baseline) and add a {ARM_COL} column",
    )
    verbosity = p.add_mutually_exclusive_group()
    verbosity.add_argument("-v", "--verbose", action="store_true")
    verbosity.add_argument("-q", "--quiet", action="store_true")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    level = logging.DEBUG if args.verbose else logging.WARNING if args.quiet else logging.INFO
    logging.basicConfig(level=level, format="%(levelname)s: %(message)s")

    output = args.output or args.input.with_name(f"{args.input.stem}_condensed.csv")
    try:
        condenser = RedcapCondenser(
            record_id=args.record_id,
            event_col=args.event_col,
            how=args.how,
            metadata_pattern=args.metadata_pattern,
            record_cols=args.record_cols,
            sep=args.sep,
            repeats=args.repeats,
            merge_arms=args.merge_arms,
        )
    except re.error as exc:
        log.error("Invalid --metadata-pattern: %s", exc)
        return 2
    try:
        condenser.condense_csv(args.input, output)
    except OSError as exc:
        log.error("%s", exc)
        return 2
    except (pd.errors.EmptyDataError, pd.errors.ParserError, UnicodeDecodeError) as exc:
        log.error("Could not read %s as a CSV export: %s", args.input, exc)
        return 1
    except RedcapCondenseError as exc:
        log.error("%s", exc)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())