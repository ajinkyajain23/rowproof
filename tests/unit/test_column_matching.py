"""Unit tests for spec §6.2 column matching — pure Python, no database.

These prove the pair-level logic (name matching, exclusion, DEC-1 min
scale, TS-2 precision upgrade) in isolation; tests/integration/
test_m1_fixtures.py then proves the *rendered SQL* that consumes this
module's output is correct against real Postgres.
"""

from tablediff.core.column_matching import match_columns
from tablediff.core.models import Column, NormalisationRule


def col(name, native_type, nullable=True, ordinal=1, scale=None, precision=None):
    return Column(
        name=name, native_type=native_type, nullable=nullable, ordinal=ordinal,
        scale=scale, precision=precision,
    )


def test_matched_columns_present_on_both_sides():
    src = {"id": col("id", "bigint"), "name": col("name", "text")}
    tgt = {"id": col("id", "bigint"), "name": col("name", "text")}
    m = match_columns(src, tgt, ["name"])
    assert m.matched == ["name"]
    assert m.excluded == []
    assert m.warnings == []


def test_case_insensitive_name_matching():
    src = {"Name": col("Name", "text")}
    tgt = {"name": col("name", "text")}
    m = match_columns(src, tgt, ["Name"])
    assert m.matched == ["Name"]
    assert m.excluded == []


def test_column_map_override():
    src = {"full_name": col("full_name", "text")}
    tgt = {"customer_name": col("customer_name", "text")}
    m = match_columns(src, tgt, ["full_name"], column_map={"full_name": "customer_name"})
    assert m.matched == ["full_name"]


def test_source_only_column_excluded_as_schema_difference():
    src = {"legacy_flag": col("legacy_flag", "boolean")}
    tgt = {}
    m = match_columns(src, tgt, ["legacy_flag"])
    assert m.matched == []
    assert m.excluded == ["legacy_flag"]
    assert len(m.warnings) == 1
    assert "only on the source side" in m.warnings[0].message


def test_incompatible_type_families_excluded_and_reported_loudly():
    src = {"created_at": col("created_at", "bigint")}
    tgt = {"created_at": col("created_at", "timestamp with time zone")}
    m = match_columns(src, tgt, ["created_at"])
    assert m.matched == []
    assert m.excluded == ["created_at"]
    assert "incompatible types" in m.warnings[0].message


def test_compatible_text_type_families_are_fine():
    # spec §6.2: "Type families differ (e.g. text vs varchar(50)): fine."
    src = {"name": col("name", "text")}
    tgt = {"name": col("name", "varchar(50)")}
    m = match_columns(src, tgt, ["name"])
    assert m.matched == ["name"]
    assert m.excluded == []


def test_unk_1_never_excludes_a_column():
    # spec §6.1 UNK-1: "Never fail a run because of an unknown type."
    src = {"blob": col("blob", "some_future_type")}
    tgt = {"blob": col("blob", "text")}
    m = match_columns(src, tgt, ["blob"])
    assert m.matched == ["blob"]
    assert m.excluded == []


def test_dec_1_uses_min_scale_of_the_pair_and_warns():
    src = {"total": col("total", "numeric(10,2)", scale=2)}
    tgt = {"total": col("total", "numeric(10,1)", scale=1)}
    m = match_columns(src, tgt, ["total"])
    assert m.matched == ["total"]
    assert m.options.scale_overrides == {"total": 1}
    assert any("DEC-1" in w.message and "scale" in w.message for w in m.warnings)
    assert m.warnings[0].rule is NormalisationRule.DEC_1


def test_dec_1_same_scale_no_warning():
    src = {"total": col("total", "numeric(10,2)", scale=2)}
    tgt = {"total": col("total", "numeric(12,2)", scale=2)}
    m = match_columns(src, tgt, ["total"])
    assert m.options.scale_overrides == {"total": 2}
    assert m.warnings == []


def test_uuid_vs_text_is_compatible_and_forces_uuid_1_on_both_sides():
    # spec §6.2: "A Postgres uuid against a Snowflake VARCHAR compares
    # after UUID-1" -- Snowflake has no native uuid type, so its side
    # reports plain text; this pairing must not be excluded as
    # "incompatible types" the way, say, int-vs-text would be.
    src = {"id": col("id", "uuid")}
    tgt = {"id": col("id", "text")}
    m = match_columns(src, tgt, ["id"])
    assert m.matched == ["id"]
    assert m.excluded == []
    assert m.rule_overrides["id"] is NormalisationRule.UUID_1


def test_uuid_vs_text_override_is_symmetric_regardless_of_which_side_is_native():
    src = {"id": col("id", "text")}
    tgt = {"id": col("id", "uuid")}
    m = match_columns(src, tgt, ["id"])
    assert m.matched == ["id"]
    assert m.rule_overrides["id"] is NormalisationRule.UUID_1


def test_uuid_vs_uuid_needs_no_override():
    src = {"id": col("id", "uuid")}
    tgt = {"id": col("id", "uuid")}
    m = match_columns(src, tgt, ["id"])
    assert m.matched == ["id"]
    assert "id" not in m.rule_overrides


def test_uuid_vs_non_string_non_uuid_still_excluded():
    # The uuid/string exception must not swallow genuinely incompatible
    # pairings -- uuid vs a numeric column stays excluded.
    src = {"id": col("id", "uuid")}
    tgt = {"id": col("id", "bigint")}
    m = match_columns(src, tgt, ["id"])
    assert m.matched == []
    assert m.excluded == ["id"]


def test_ts_2_upgrade_when_precision_differs():
    src = {"updated_at": col("updated_at", "timestamp with time zone", precision=6)}
    tgt = {"updated_at": col("updated_at", "timestamp with time zone", precision=0)}
    m = match_columns(src, tgt, ["updated_at"])
    assert m.matched == ["updated_at"]
    assert m.rule_overrides["updated_at"] is NormalisationRule.TS_2
    assert m.options.precision_overrides == {"updated_at": 0}
    assert any(w.rule is NormalisationRule.TS_2 for w in m.warnings)


def test_no_ts_2_upgrade_when_precision_matches():
    src = {"updated_at": col("updated_at", "timestamp with time zone", precision=6)}
    tgt = {"updated_at": col("updated_at", "timestamp with time zone", precision=6)}
    m = match_columns(src, tgt, ["updated_at"])
    assert "updated_at" not in m.rule_overrides
    assert m.warnings == []


def test_str_2_promotion_when_trim_or_case_insensitive_active():
    from tablediff.core.models import NormaliseOptions

    src = {"name": col("name", "text")}
    tgt = {"name": col("name", "text")}
    m = match_columns(src, tgt, ["name"], base_options=NormaliseOptions(trim=True))
    assert m.rule_overrides["name"] is NormalisationRule.STR_2


def test_no_str_2_promotion_by_default():
    src = {"name": col("name", "text")}
    tgt = {"name": col("name", "text")}
    m = match_columns(src, tgt, ["name"])
    assert "name" not in m.rule_overrides
