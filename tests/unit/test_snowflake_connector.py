"""Unit tests for SnowflakeConnector's pure logic: type resolution (the
Snowflake information_schema `data_type` -> pick_rule() sentinel mapping)
and schema/primary-key parsing against a scripted `query()` — none of
this needs a live server, it's all string/data manipulation on
Snowflake's documented information_schema.columns shape (empirically
confirmed against a real trial account, see snowflake.py's own module
docstring). What it does NOT cover — the generated SQL actually meaning
what this file assumes when Snowflake executes it — is exactly what
tests/integration/test_snowflake_real.py is about instead.
"""

from __future__ import annotations

from tablediff.connectors.snowflake import SnowflakeConnector, _resolve
from tablediff.core.models import Column, NormaliseOptions, TableRef
from tablediff.core.normalisation import pick_rule


class TestResolve:
    def test_number_with_zero_scale_maps_to_int_1(self):
        # spec §9/§13 M3: "NUMBER(38,0) as integer"
        sentinel, scale, precision = _resolve("NUMBER", 0, None)
        assert pick_rule(sentinel).value == "INT-1"
        assert scale is None

    def test_number_with_nonzero_scale_maps_to_dec_1_and_reports_scale(self):
        sentinel, scale, _ = _resolve("NUMBER", 2, None)
        assert pick_rule(sentinel).value == "DEC-1"
        assert scale == 2

    def test_number_with_no_reported_scale_defaults_to_int_1(self):
        sentinel, scale, _ = _resolve("NUMBER", None, None)
        assert pick_rule(sentinel).value == "INT-1"

    def test_float_maps_to_flt_1(self):
        sentinel, _, _ = _resolve("FLOAT", None, None)
        assert pick_rule(sentinel).value == "FLT-1"

    def test_text_maps_to_str_1(self):
        sentinel, _, _ = _resolve("TEXT", None, None)
        assert pick_rule(sentinel).value == "STR-1"

    def test_boolean_maps_to_bool_1(self):
        sentinel, _, _ = _resolve("BOOLEAN", None, None)
        assert pick_rule(sentinel).value == "BOOL-1"

    def test_date_maps_to_date_1(self):
        sentinel, _, _ = _resolve("DATE", None, None)
        assert pick_rule(sentinel).value == "DATE-1"

    def test_time_maps_to_time_1_and_reports_precision(self):
        sentinel, _, precision = _resolve("TIME", None, 6)
        assert pick_rule(sentinel).value == "TIME-1"
        assert precision == 6

    def test_timestamp_ntz_maps_to_ts_3_naive(self):
        # spec §9/§13 M3: "TIMESTAMP_NTZ handled with TS-3 warning"
        sentinel, _, precision = _resolve("TIMESTAMP_NTZ", None, 9)
        assert pick_rule(sentinel).value == "TS-3"
        assert precision == 9

    def test_timestamp_ltz_maps_to_ts_1_tz_aware(self):
        sentinel, _, _ = _resolve("TIMESTAMP_LTZ", None, 6)
        assert pick_rule(sentinel).value == "TS-1"

    def test_timestamp_tz_maps_to_ts_1_tz_aware(self):
        # spec §9/§13 M3: "TIMESTAMP_TZ converts to UTC"
        sentinel, _, _ = _resolve("TIMESTAMP_TZ", None, 6)
        assert pick_rule(sentinel).value == "TS-1"

    def test_binary_maps_to_bin_1(self):
        sentinel, _, _ = _resolve("BINARY", None, None)
        assert pick_rule(sentinel).value == "BIN-1"

    def test_variant_falls_back_to_unk_1_per_spec(self):
        # spec §9/§13 M3: "VARIANT as text"
        sentinel, _, _ = _resolve("VARIANT", None, None)
        assert pick_rule(sentinel).value == "UNK-1"

    def test_array_and_object_fall_back_to_unk_1(self):
        assert pick_rule(_resolve("ARRAY", None, None)[0]).value == "UNK-1"
        assert pick_rule(_resolve("OBJECT", None, None)[0]).value == "UNK-1"

    def test_unmapped_type_falls_back_to_unk_1_and_never_raises(self):
        sentinel, _, _ = _resolve("GEOGRAPHY", None, None)
        assert pick_rule(sentinel).value == "UNK-1"


class TestGetSchemaAndPrimaryKey:
    def _connector_with_scripted_query(self, rows):
        c = SnowflakeConnector()
        c.query = lambda sql, params=None: rows
        return c

    def test_get_schema_maps_information_schema_rows_into_columns(self):
        c = self._connector_with_scripted_query([
            ("ID", "NUMBER", "NO", 1, 0, None),
            ("AMOUNT", "NUMBER", "YES", 2, 2, None),
            ("CREATED_AT", "TIMESTAMP_TZ", "YES", 3, None, 6),
            ("NOTE", "TEXT", "YES", 4, None, None),
        ])
        columns = c.get_schema(TableRef(engine="snowflake", database="db", table="t", schema="PUBLIC"))

        by_name = {col.name: col for col in columns}
        assert by_name["ID"].native_type == "bigint"
        assert by_name["ID"].nullable is False

        assert by_name["AMOUNT"].native_type == "numeric"
        assert by_name["AMOUNT"].nullable is True
        assert by_name["AMOUNT"].scale == 2

        assert by_name["CREATED_AT"].native_type == "timestamp with time zone"
        assert by_name["CREATED_AT"].precision == 6

        assert by_name["NOTE"].native_type == "text"

    def test_get_primary_key_returns_none_when_no_rows(self):
        c = self._connector_with_scripted_query([])
        c._dsn = type("D", (), {"database": "db"})()
        assert c.get_primary_key(TableRef(engine="snowflake", database="db", table="t", schema="PUBLIC")) is None

    def test_get_primary_key_returns_names_ordered_by_key_sequence(self):
        # SHOW PRIMARY KEYS IN TABLE row shape (confirmed against a real
        # account): created_on, database_name, schema_name, table_name,
        # column_name, key_sequence, constraint_name, rely, comment.
        c = self._connector_with_scripted_query([
            (None, "db", "PUBLIC", "t", "TS", 2, "c", "false", None),
            (None, "db", "PUBLIC", "t", "ID", 1, "c", "false", None),
        ])
        c._dsn = type("D", (), {"database": "db"})()
        assert c.get_primary_key(TableRef(engine="snowflake", database="db", table="t", schema="PUBLIC")) == ["ID", "TS"]


class TestNormaliseExprShape:
    """String-shape assertions only (no engine to execute against here) —
    proves the generator doesn't crash and produces recognisable SQL
    skeletons; the actual correctness of this SQL against real Snowflake
    is proven empirically in snowflake.py's own module docstring plus
    tests/integration/test_snowflake_real.py."""

    def test_int_1_wraps_null_check_when_nullable(self):
        from tablediff.core.models import NormalisationRule

        c = SnowflakeConnector()
        col = Column(name="n", native_type="bigint", nullable=True, ordinal=1)
        expr = c.normalise_expr(col, NormalisationRule.INT_1, NormaliseOptions())
        assert "IS NULL" in expr
        assert "'\\\\N'" in expr
        assert 'TO_VARCHAR("n")' in expr

    def test_variant_type_falls_through_to_to_varchar_fallback(self):
        from tablediff.core.models import NormalisationRule

        c = SnowflakeConnector()
        col = Column(name="v", native_type="variant", nullable=False, ordinal=1)
        expr = c.normalise_expr(col, NormalisationRule.UNK_1, NormaliseOptions())
        assert expr == 'TO_VARCHAR("v")'

    def test_uuid_1_reassembles_bare_32_char_hex_and_lowercases(self):
        from tablediff.core.models import NormalisationRule

        c = SnowflakeConnector()
        col = Column(name="uid", native_type="text", nullable=False, ordinal=1)
        expr = c.normalise_expr(col, NormalisationRule.UUID_1, NormaliseOptions())
        assert "LOWER(IFF(LENGTH" in expr
        assert "SUBSTR" in expr

    def test_row_hash_expr_single_column_skips_separator(self):
        c = SnowflakeConnector()
        expr = c.row_hash_expr(["only_col"])
        assert "CHR(31)" not in expr
        assert "only_col" in expr

    def test_row_hash_expr_multi_column_joins_with_unit_separator(self):
        c = SnowflakeConnector()
        expr = c.row_hash_expr(["a", "b", "c"])
        assert expr.count("CHR(31)") == 2

    def test_row_hash_expr_folds_unsigned_to_signed_range(self):
        c = SnowflakeConnector()
        expr = c.row_hash_expr(["x"])
        assert "9223372036854775808" in expr
        assert "18446744073709551616" in expr

    def test_quote_identifier_escapes_double_quote(self):
        c = SnowflakeConnector()
        assert c.quote_identifier('weird"col') == '"weird""col"'

    def test_quote_literal_escapes_quotes_and_backslashes(self):
        c = SnowflakeConnector()
        assert c.quote_literal("o'brien") == "'o''brien'"
        assert c.quote_literal("a\\b") == "'a\\\\b'"
        assert c.quote_literal(None) == "NULL"
        assert c.quote_literal(True) == "true"
        assert c.quote_literal(42) == "42"
