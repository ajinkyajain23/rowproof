"""Unit tests for ClickHouseConnector's pure logic: type resolution
(Nullable/LowCardinality unwrapping, the ClickHouse-type -> pick_rule()
sentinel mapping) and schema/primary-key parsing against a scripted
`query()` — none of this needs a live server, it's all string/data
manipulation on ClickHouse's documented `system.columns` shape. What it
does NOT cover — the generated SQL actually meaning what this file
assumes when ClickHouse executes it — is exactly what
tests/unit/test_clickhouse_hash_reference.py and
tests/integration/test_clickhouse_real.py are about instead.
"""

from __future__ import annotations

from rowproof.connectors.clickhouse import ClickHouseConnector, _resolve, _unwrap
from rowproof.core.models import NormaliseOptions, TableRef
from rowproof.core.normalisation import pick_rule


class TestUnwrap:
    def test_bare_type_is_not_nullable(self):
        assert _unwrap("String") == ("String", False)

    def test_nullable_wrapper_strips_and_flags_nullable(self):
        assert _unwrap("Nullable(String)") == ("String", True)

    def test_low_cardinality_wrapper_strips_without_flagging_nullable(self):
        assert _unwrap("LowCardinality(String)") == ("String", False)

    def test_low_cardinality_of_nullable_strips_both_and_flags_nullable(self):
        assert _unwrap("LowCardinality(Nullable(String))") == ("String", True)

    def test_wrapper_stripping_does_not_touch_unrelated_parens(self):
        assert _unwrap("Decimal(12, 4)") == ("Decimal(12, 4)", False)
        assert _unwrap("Nullable(Decimal(12, 4))") == ("Decimal(12, 4)", True)


class TestResolve:
    def test_every_int_width_and_signedness_maps_to_int_1(self):
        for name in ["UInt8", "UInt16", "UInt32", "UInt64", "UInt128", "UInt256",
                     "Int8", "Int16", "Int32", "Int64", "Int128", "Int256"]:
            sentinel, scale, precision = _resolve(name)
            assert pick_rule(sentinel).value == "INT-1", name

    def test_float32_and_float64_map_to_flt_1(self):
        for name in ["Float32", "Float64"]:
            sentinel, _, _ = _resolve(name)
            assert pick_rule(sentinel).value == "FLT-1", name

    def test_decimal_reports_scale_and_maps_to_dec_1(self):
        sentinel, scale, precision = _resolve("Decimal(12, 4)")
        assert pick_rule(sentinel).value == "DEC-1"
        assert scale == 4
        assert precision is None

    def test_decimal_with_no_explicit_scale_defaults_to_zero(self):
        _, scale, _ = _resolve("Decimal(12)")
        assert scale == 0

    def test_fixed_width_decimal_variants_report_their_scale(self):
        assert _resolve("Decimal32(2)")[1] == 2
        assert _resolve("Decimal64(6)")[1] == 6
        assert _resolve("Decimal128(10)")[1] == 10
        assert _resolve("Decimal256(20)")[1] == 20

    def test_datetime_maps_to_ts_1_family_with_precision_zero(self):
        sentinel, scale, precision = _resolve("DateTime")
        assert pick_rule(sentinel).value == "TS-1"
        assert precision == 0

    def test_datetime64_reports_its_precision_and_maps_to_ts_1_family(self):
        sentinel, scale, precision = _resolve("DateTime64(3)")
        assert pick_rule(sentinel).value == "TS-1"
        assert precision == 3

    def test_datetime64_with_timezone_argument_still_parses_precision(self):
        _, _, precision = _resolve("DateTime64(6, 'UTC')")
        assert precision == 6

    def test_date_and_date32_map_to_date_1(self):
        for name in ["Date", "Date32"]:
            sentinel, _, _ = _resolve(name)
            assert pick_rule(sentinel).value == "DATE-1", name

    def test_uuid_maps_to_uuid_1(self):
        sentinel, _, _ = _resolve("UUID")
        assert pick_rule(sentinel).value == "UUID-1"

    def test_string_and_fixedstring_map_to_str_1(self):
        assert pick_rule(_resolve("String")[0]).value == "STR-1"
        assert pick_rule(_resolve("FixedString(16)")[0]).value == "STR-1"

    def test_bool_maps_to_bool_1(self):
        assert pick_rule(_resolve("Bool")[0]).value == "BOOL-1"

    def test_enum8_and_enum16_map_to_enum_1(self):
        assert pick_rule(_resolve("Enum8('a' = 1, 'b' = 2)")[0]).value == "ENUM-1"
        assert pick_rule(_resolve("Enum16('a' = 1)")[0]).value == "ENUM-1"

    def test_array_maps_to_arr_1(self):
        assert pick_rule(_resolve("Array(Int32)")[0]).value == "ARR-1"

    def test_map_falls_back_to_unk_1_per_spec(self):
        # spec §9 M2: "LowCardinality, Enum8/16, Array, Map (as text, UNK-1) types"
        assert pick_rule(_resolve("Map(String, Int32)")[0]).value == "UNK-1"

    def test_tuple_falls_back_to_unk_1(self):
        assert pick_rule(_resolve("Tuple(Int32, String)")[0]).value == "UNK-1"


class TestGetSchemaAndPrimaryKey:
    def _connector_with_scripted_query(self, rows):
        c = ClickHouseConnector()
        c.query = lambda sql, params=None: rows
        return c

    def test_get_schema_maps_system_columns_rows_into_columns(self):
        c = self._connector_with_scripted_query([
            ("id", "UInt64", 1),
            ("amount", "Nullable(Decimal(12, 4))", 2),
            ("created_at", "DateTime64(3)", 3),
            ("label", "LowCardinality(String)", 4),
        ])
        columns = c.get_schema(TableRef(engine="clickhouse", database="db", table="t", schema="db"))

        by_name = {col.name: col for col in columns}
        assert by_name["id"].native_type == "bigint"
        assert by_name["id"].nullable is False

        assert by_name["amount"].native_type == "decimal"
        assert by_name["amount"].nullable is True
        assert by_name["amount"].scale == 4

        assert by_name["created_at"].native_type == "timestamp with time zone"
        assert by_name["created_at"].precision == 3

        assert by_name["label"].native_type == "text"
        assert by_name["label"].nullable is False

    def test_get_primary_key_returns_none_when_no_rows(self):
        c = self._connector_with_scripted_query([])
        assert c.get_primary_key(TableRef(engine="clickhouse", database="db", table="t", schema="db")) is None

    def test_get_primary_key_returns_names_in_order(self):
        c = self._connector_with_scripted_query([("id",), ("ts",)])
        assert c.get_primary_key(TableRef(engine="clickhouse", database="db", table="t", schema="db")) == ["id", "ts"]


class TestNormaliseExprShape:
    """String-shape assertions only (no engine to execute against) —
    proves the generator doesn't crash and produces recognisable SQL
    skeletons; NOT proof the SQL is correct ClickHouse (see module
    docstring)."""

    def test_int_1_wraps_null_check_when_nullable(self):
        from rowproof.core.models import Column, NormalisationRule
        c = ClickHouseConnector()
        col = Column(name="n", native_type="bigint", nullable=True, ordinal=1)
        expr = c.normalise_expr(col, NormalisationRule.INT_1, NormaliseOptions())
        assert "IS NULL" in expr
        assert "char(92), 'N'" in expr
        assert "toString(`n`)" in expr

    def test_row_hash_expr_single_column_skips_separator(self):
        c = ClickHouseConnector()
        expr = c.row_hash_expr(["only_col"])
        assert "char(31)" not in expr
        assert "only_col" in expr

    def test_row_hash_expr_multi_column_joins_with_unit_separator(self):
        c = ClickHouseConnector()
        expr = c.row_hash_expr(["a", "b", "c"])
        assert expr.count("char(31)") == 2

    def test_quote_identifier_escapes_backtick(self):
        c = ClickHouseConnector()
        assert c.quote_identifier("weird`col") == "`weird``col`"

    def test_quote_literal_escapes_quotes_and_backslashes(self):
        c = ClickHouseConnector()
        assert c.quote_literal("o'brien") == "'o\\'brien'"
        assert c.quote_literal(None) == "NULL"
        assert c.quote_literal(True) == "true"
        assert c.quote_literal(42) == "42"
