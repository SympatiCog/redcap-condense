import re

import pandas as pd
import pytest

from redcap_condense import (
    RedcapCondenseError,
    condense_redcap,
    condense_redcap_csv,
    main,
)

EXPORT = """recordid,redcap_event_name,age,sex,weight,moca,notes
0001,baseline_arm_1,70,1,80.5,,
0001,followup_arm_1,,,79.0,27,
0001,admin_arm_1,,,,,
0002,baseline_arm_1,65,2,60.0,,
0003,baseline_arm_1,72,1,90.1,,
0003,followup_arm_1,,,88.0,25,
"""


@pytest.fixture
def export_csv(tmp_path):
    path = tmp_path / "export.csv"
    path.write_text(EXPORT)
    return path


def test_outer_join_keeps_all_records(export_csv):
    wide = condense_redcap_csv(export_csv)
    assert list(wide["recordid"]) == ["0001", "0002", "0003"]  # leading zeros kept
    assert wide.loc[wide.recordid == "0002", "moca"].isna().all()


def test_shared_fields_get_event_suffix(export_csv):
    wide = condense_redcap_csv(export_csv)
    assert {"weight__baseline_arm_1", "weight__followup_arm_1"} <= set(wide.columns)
    assert "weight" not in wide.columns
    assert "age" in wide.columns  # unique field keeps its name


def test_empty_event_and_columns_dropped(export_csv):
    wide = condense_redcap_csv(export_csv)
    assert "notes" not in wide.columns
    assert not any("admin_arm_1" in c for c in wide.columns)
    assert "redcap_event_name" not in wide.columns


def test_inner_join(export_csv):
    wide = condense_redcap_csv(export_csv, how="inner")
    assert list(wide["recordid"]) == ["0001", "0003"]


def test_integer_codes_stay_integers(export_csv, tmp_path):
    out = tmp_path / "out.csv"
    condense_redcap_csv(export_csv, out)
    text = out.read_text()
    assert "27.0" not in text and ",27" in text


def test_repeating_rows_error_or_drop():
    df = pd.DataFrame({
        "record_id": ["1", "1", "1"],
        "redcap_event_name": ["bl", "bl", "bl"],
        "redcap_repeat_instrument": [None, "meds", "meds"],
        "redcap_repeat_instance": [None, 1, 2],
        "age": [70, None, None],
        "med": [None, "a", "b"],
    })
    with pytest.raises(RedcapCondenseError, match="repeating"):
        condense_redcap(df)
    wide = condense_redcap(df, repeats="drop")
    assert list(wide.columns) == ["record_id", "age"]


def test_duplicate_ids_in_event_raise():
    df = pd.DataFrame({"id": ["1", "1"], "redcap_event_name": ["bl", "bl"], "x": [1, 2]})
    with pytest.raises(RedcapCondenseError, match="Duplicate"):
        condense_redcap(df)


DAG_EXPORT = """recordid,redcap_event_name,redcap_data_access_group,age,demo_complete,moca,cog_complete,cog_timestamp
0001,baseline_arm_1,site_a,70,2,,0,
0001,followup_arm_1,site_a,,0,27,2,2026-01-05 10:00
0001,admin_arm_1,site_a,,0,,0,
0002,baseline_arm_1,site_b,65,2,,0,
0002,admin_arm_1,site_b,,0,,0,
"""


@pytest.fixture
def dag_csv(tmp_path):
    path = tmp_path / "dag.csv"
    path.write_text(DAG_EXPORT)
    return path


def test_bookkeeping_only_event_dropped(dag_csv):
    wide = condense_redcap_csv(dag_csv)
    assert not any(c.endswith("admin_arm_1") for c in wide.columns)
    # Bookkeeping columns of kept events stay, suffixed when shared.
    assert {"demo_complete__baseline_arm_1", "cog_complete__followup_arm_1"} <= set(wide.columns)
    assert "cog_timestamp" in wide.columns


def test_dag_is_one_column_after_id(dag_csv):
    wide = condense_redcap_csv(dag_csv)
    assert list(wide.columns[:2]) == ["recordid", "redcap_data_access_group"]
    assert list(wide["redcap_data_access_group"]) == ["site_a", "site_b"]
    assert sum("data_access_group" in c for c in wide.columns) == 1


def test_dag_does_not_change_inner_join(dag_csv):
    wide = condense_redcap_csv(dag_csv, how="inner")
    assert list(wide["recordid"]) == ["0001"]
    assert wide["redcap_data_access_group"].tolist() == ["site_a"]


def test_empty_pattern_counts_everything(dag_csv):
    wide = condense_redcap_csv(dag_csv, metadata_pattern="")
    assert any(c.endswith("admin_arm_1") for c in wide.columns)


def test_custom_pattern_and_record_cols(dag_csv):
    # Treat moca as bookkeeping: follow-up then has no data and is dropped.
    wide = condense_redcap_csv(dag_csv, metadata_pattern=r"_complete$|_timestamp$|^moca$")
    assert "moca" not in wide.columns
    # With no record cols, the DAG becomes an ordinary per-event column.
    wide = condense_redcap_csv(dag_csv, record_cols=())
    assert "redcap_data_access_group__baseline_arm_1" in wide.columns


def test_bad_pattern_fails_early():
    with pytest.raises(re.error):
        condense_redcap(pd.DataFrame(), metadata_pattern="(")


def test_cli_options(dag_csv, tmp_path):
    out = tmp_path / "wide.csv"
    assert main([str(dag_csv), str(out), "-q", "--metadata-pattern", ""]) == 0
    assert any(c.endswith("admin_arm_1") for c in pd.read_csv(out).columns)


def test_cli(export_csv, tmp_path):
    out = tmp_path / "wide.csv"
    assert main([str(export_csv), str(out), "-q"]) == 0
    assert pd.read_csv(out).shape[0] == 3
    assert main([str(tmp_path / "missing.csv"), "-q"]) == 2


MULTI_ARM = """record_id,redcap_event_name,weight,dose
0001,baseline_arm_1,80,
0001,followup_arm_1,79,10
0002,baseline_arm_2,60,
0002,followup_arm_2,61,20
"""


@pytest.fixture
def multi_arm_csv(tmp_path):
    path = tmp_path / "multi_arm.csv"
    path.write_text(MULTI_ARM)
    return path


def test_arms_kept_apart_by_default(multi_arm_csv):
    wide = condense_redcap_csv(multi_arm_csv)
    assert {"weight__baseline_arm_1", "weight__baseline_arm_2"} <= set(wide.columns)


def test_merge_arms(multi_arm_csv):
    wide = condense_redcap_csv(multi_arm_csv, merge_arms=True)
    assert list(wide.columns) == [
        "record_id", "redcap_arm", "weight__baseline", "weight__followup", "dose",
    ]
    assert wide["redcap_arm"].tolist() == [1, 2]
    assert wide["weight__followup"].tolist() == [79, 61]


def test_merge_arms_inner_keeps_all_arms(multi_arm_csv):
    assert condense_redcap_csv(multi_arm_csv, how="inner").empty
    wide = condense_redcap_csv(multi_arm_csv, how="inner", merge_arms=True)
    assert wide["record_id"].tolist() == ["0001", "0002"]


def test_merge_arms_rejects_record_in_two_arms():
    df = pd.DataFrame({
        "id": ["1", "1"],
        "redcap_event_name": ["bl_arm_1", "bl_arm_2"],
        "x": [1, 2],
    })
    with pytest.raises(RedcapCondenseError, match="more than one arm"):
        condense_redcap(df, merge_arms=True)


def test_merge_arms_rejects_event_without_arm():
    df = pd.DataFrame({"id": ["1"], "redcap_event_name": ["baseline"], "x": [1]})
    with pytest.raises(RedcapCondenseError, match="_arm_N"):
        condense_redcap(df, merge_arms=True)


def test_empty_result_warns(multi_arm_csv, caplog):
    condense_redcap_csv(multi_arm_csv, how="inner")
    assert "No records in the output" in caplog.text


def test_unchecked_checkboxes_do_not_keep_event():
    df = pd.DataFrame({
        "id": ["1", "1"],
        "redcap_event_name": ["bl_arm_1", "fu_arm_1"],
        "age": [70, None],
        "sym___1": [1, 0],
        "sym___2": [0, 0],
    })
    wide = condense_redcap(df)
    assert list(wide.columns) == ["id", "age", "sym___1", "sym___2"]
    # A checked box does count as data.
    df.loc[1, "sym___2"] = 1
    assert "sym___2__fu_arm_1" in condense_redcap(df).columns


def test_rows_without_event_dropped_with_warning(caplog):
    df = pd.DataFrame({
        "id": ["1", "1"],
        "redcap_event_name": ["bl_arm_1", None],
        "x": [1, 2],
    })
    wide = condense_redcap(df)
    assert wide["x"].tolist() == [1]
    assert "1 rows with no redcap_event_name" in caplog.text


def test_suffix_collision_raises():
    df = pd.DataFrame({
        "id": ["1", "1"],
        "redcap_event_name": ["a", "b"],
        "w": [1, 2],
        "w__b": [None, 9],
    })
    with pytest.raises(RedcapCondenseError, match="Duplicate output column"):
        condense_redcap(df)
    assert "w--b" in condense_redcap(df, sep="--").columns


def test_values_align_by_record_not_row_order():
    df = pd.DataFrame({
        "id": ["3", "1", "2", "1", "3"],
        "redcap_event_name": ["a", "b", "a", "a", "b"],
        "x": [30, None, 20, 10, None],
        "y": [None, 100, None, None, 300],
    })
    wide = condense_redcap(df).set_index("id")
    assert wide.loc["1", "x"] == 10 and wide.loc["1", "y"] == 100
    assert wide.loc["3", "x"] == 30 and wide.loc["3", "y"] == 300
    assert pd.isna(wide.loc["2", "y"])


def test_label_export_hint():
    df = pd.DataFrame({"Record ID": ["1", "1"], "Event Name": ["Baseline", "Follow-up"]})
    with pytest.raises(RedcapCondenseError, match="raw variable names"):
        condense_redcap(df)


def test_cli_record_cols_comma_list(dag_csv, tmp_path):
    out = tmp_path / "wide.csv"
    assert main([str(dag_csv), "--record-cols", "", str(out), "-q"]) == 0
    assert "redcap_data_access_group__baseline_arm_1" in pd.read_csv(out).columns


def test_cli_merge_arms(multi_arm_csv, tmp_path):
    out = tmp_path / "wide.csv"
    assert main([str(multi_arm_csv), str(out), "--merge-arms", "-q"]) == 0
    assert "weight__baseline" in pd.read_csv(out).columns


def test_cli_errors_are_clean(tmp_path, capsys):
    empty = tmp_path / "empty.csv"
    empty.write_text("")
    assert main([str(empty), "-q"]) == 1
    good = tmp_path / "ok.csv"
    good.write_text(MULTI_ARM)
    assert main([str(good), "-q", "--metadata-pattern", "("]) == 2
    assert main([str(good), str(tmp_path), "-q"]) == 2  # output is a directory
    assert "Traceback" not in capsys.readouterr().err
