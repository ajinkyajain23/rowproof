"""Unit tests for rule selection (spec §6.1, §6.2) — pure Python, no
database. This only tests which rule ID a native type *picks*; the actual
canonical-string SQL each rule renders (and the DEC-1/TS-2 pair-level
scale/precision math) is verified against real Postgres in
tests/integration/test_m1_fixtures.py per spec §6.4.
"""

from tablediff.core.models import NormalisationRule
from tablediff.core.normalisation import pick_rule


def test_integer_types_pick_int_1():
    for native_type in ["bigint", "integer", "int", "int4", "int8", "smallint", "serial"]:
        assert pick_rule(native_type) is NormalisationRule.INT_1


def test_text_types_pick_str_1():
    for native_type in [
        "text",
        "character varying",
        "character varying(50)",
        "varchar(50)",
        "character(10)",
        "name",
    ]:
        assert pick_rule(native_type) is NormalisationRule.STR_1


def test_uuid_picks_uuid_1():
    # M1: uuid gets its own dedicated rule ID (UUID-1) rather than the M0
    # STR-1 stand-in, even though the rendering SQL is the same text cast.
    assert pick_rule("uuid") is NormalisationRule.UUID_1


def test_decimal_types_pick_dec_1():
    for native_type in ["numeric", "decimal", "numeric(10,2)", "decimal(12,4)", "NUMERIC(5,0)"]:
        assert pick_rule(native_type) is NormalisationRule.DEC_1


def test_float_types_pick_flt_1():
    for native_type in ["real", "double precision", "float4", "float8"]:
        assert pick_rule(native_type) is NormalisationRule.FLT_1


def test_boolean_types_pick_bool_1():
    assert pick_rule("boolean") is NormalisationRule.BOOL_1
    assert pick_rule("bool") is NormalisationRule.BOOL_1


def test_date_picks_date_1():
    assert pick_rule("date") is NormalisationRule.DATE_1


def test_time_types_pick_time_1():
    for native_type in ["time", "time without time zone", "time with time zone", "time(6)"]:
        assert pick_rule(native_type) is NormalisationRule.TIME_1


def test_timestamp_with_time_zone_picks_ts_1():
    for native_type in ["timestamp with time zone", "timestamptz"]:
        assert pick_rule(native_type) is NormalisationRule.TS_1


def test_naive_timestamp_picks_ts_3():
    # M1: a timestamp with no tz is TS-3 ("assumed UTC, warn once per
    # run"), distinct from TS-1's always-has-a-real-UTC-offset case. M0
    # lumped both under TS-1 since the rendering happened to coincide;
    # M1 needs them distinguished so TS-3's warning fires correctly and
    # TS-2 (precision-differs) can upgrade either one.
    for native_type in ["timestamp without time zone", "timestamp", "timestamp(3)"]:
        assert pick_rule(native_type) is NormalisationRule.TS_3


def test_bytea_picks_bin_1():
    assert pick_rule("bytea") is NormalisationRule.BIN_1


def test_json_types_pick_json_1():
    assert pick_rule("json") is NormalisationRule.JSON_1
    assert pick_rule("jsonb") is NormalisationRule.JSON_1


def test_enum_sentinel_picks_enum_1():
    # PostgresConnector.get_schema() rewrites data_type "USER-DEFINED"
    # enum columns to this sentinel before pick_rule ever sees it.
    assert pick_rule("enum") is NormalisationRule.ENUM_1


def test_array_sentinel_picks_arr_1():
    # PostgresConnector.get_schema() rewrites data_type "ARRAY" columns
    # to "<elem>[]" before pick_rule ever sees it.
    for native_type in ["integer[]", "text[]", "uuid[]"]:
        assert pick_rule(native_type) is NormalisationRule.ARR_1


def test_unmapped_type_falls_back_to_unk_1_and_never_raises():
    assert pick_rule("some_future_type_nobody_has_heard_of") is NormalisationRule.UNK_1


def test_type_matching_is_case_insensitive():
    assert pick_rule("BIGINT") is NormalisationRule.INT_1
    assert pick_rule("Timestamp With Time Zone") is NormalisationRule.TS_1
