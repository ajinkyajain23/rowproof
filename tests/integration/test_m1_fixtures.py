"""M1 fixture suite (spec §6.4), verified against real Postgres — spec
§13: "Verify on real databases before calling a milestone done."

Each class below corresponds to one §6.4 fixture category. Most tests call
`Connector.normalise_expr()` directly (through `render()`) to prove the
exact canonical string a single column renders to, matching how the
fixture list itself is phrased ("a value, its native type ... and the
expected canonical string"). The pair-level rules (DEC-1's min scale,
TS-2's min precision) additionally go through `core.column_matching` and
`core.hashdiff.diff()` end to end, since those two rules are only correct
once *both* sides' schemas are known.
"""

from __future__ import annotations

import re

import pytest

from rowproof.connectors.postgres import PostgresConnector
from rowproof.core.column_matching import match_columns
from rowproof.core.hashdiff import diff
from rowproof.core.models import NormalisationRule, NormaliseOptions, TableRef
from rowproof.core.normalisation import pick_rule

from .conftest import exec_sql


def table_ref(db: str, table: str) -> TableRef:
    return TableRef(engine="postgres", database=db, schema="public", table=table)


def connector_for(dsn_url: str) -> PostgresConnector:
    c = PostgresConnector()
    c.connect(dsn_url)
    return c


def render(dsn_url: str, table: str, col_name: str = "v", options: NormaliseOptions | None = None) -> list:
    """Render every row's `col_name` through normalise_expr(), in id order."""
    conn = connector_for(dsn_url)
    cols = {c.name: c for c in conn.get_schema(table_ref("db", table))}
    column = cols[col_name]
    rule = pick_rule(column.native_type)
    expr = conn.normalise_expr(column, rule, options)
    rows = conn.query(f"SELECT {expr} FROM {table} ORDER BY id")
    return [row[0] for row in rows]


class TestTimestampFixtures:
    def test_tz_aware_converts_to_utc_iso8601(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v timestamptz)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (TIMESTAMPTZ '2024-03-01 10:00:00+05:30')")
        assert render(pg_database, "t") == ["2024-03-01T04:30:00.000000Z"]

    def test_naive_timestamp_rendered_as_is_with_z_suffix_and_warns_once(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, v timestamp)")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, TIMESTAMP '2024-03-01 10:00:00.123456')")
        exec_sql(pg_database, "CREATE TABLE t (LIKE s INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO t SELECT * FROM s")
        assert render(pg_database, "s") == ["2024-03-01T10:00:00.123456Z"]

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match
        ts3 = [w for w in result.warnings if w.rule is NormalisationRule.TS_3]
        assert len(ts3) == 1, result.warnings  # "warn once per run", not once per column/row

    def test_min_and_max_timestamps(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v timestamptz)")
        exec_sql(
            pg_database,
            "INSERT INTO t (v) VALUES (TIMESTAMPTZ '0001-01-01 00:00:00+00'), "
            "(TIMESTAMPTZ '9999-12-31 23:59:59+00')",
        )
        rows = render(pg_database, "t")
        assert rows == ["0001-01-01T00:00:00.000000Z", "9999-12-31T23:59:59.000000Z"]


class TestTs2PrecisionMismatch:
    def _make_pair(self, pg_database, src_val: str, tgt_val: str):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, updated_at timestamp(6) with time zone)")
        exec_sql(pg_database, f"INSERT INTO s VALUES (1, TIMESTAMPTZ {src_val!r})")
        exec_sql(pg_database, "CREATE TABLE t (id bigint PRIMARY KEY, updated_at timestamp(0) with time zone)")
        exec_sql(pg_database, f"INSERT INTO t VALUES (1, TIMESTAMPTZ {tgt_val!r})")
        return connector_for(pg_database), connector_for(pg_database)

    def test_truncating_to_lower_precision_makes_same_second_a_match(self, pg_database):
        src, tgt = self._make_pair(
            pg_database, "2024-03-01 10:00:00.123456+00", "2024-03-01 10:00:00+00"
        )
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match, result.row_diffs
        assert any(w.rule is NormalisationRule.TS_2 for w in result.warnings)

    def test_reports_changed_with_ts2_citation_when_truncated_values_still_differ(self, pg_database):
        src, tgt = self._make_pair(
            pg_database, "2024-03-01 10:00:00.123456+00", "2024-03-01 10:00:01+00"
        )
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert not result.is_match
        assert result.changed == 1
        row = result.row_diffs[0]
        _, _, rule = row.changes["updated_at"]
        assert rule is NormalisationRule.TS_2

    def test_rounds_half_up_at_lower_precision_not_truncates(self, pg_database):
        # .999999 rounds UP into the next second at precision 0 — this is
        # exactly the case that distinguishes rounding from truncation
        # (truncating would give 23:59:59, which would NOT match).
        src, tgt = self._make_pair(
            pg_database, "2024-03-01 23:59:59.999999+00", "2024-03-02 00:00:00+00"
        )
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match, result.row_diffs


class TestTs2TimePrecisionMismatch:
    def test_time_precision_pair_rounds_half_up_and_matches(self, pg_database):
        # Same rule (TS-2), applied to a `time`-only pair rather than a
        # timestamp pair. Postgres's own time(3) storage rounds (verified
        # empirically: '13:45:07.9995'::time(3) -> '13:45:08', not
        # '13:45:07.999') — TS-2's rendering must agree with that, not
        # truncate, or a genuinely identical value would misreport as
        # changed.
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, started_at time(6))")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, TIME '13:45:07.9995')")
        exec_sql(pg_database, "CREATE TABLE t (id bigint PRIMARY KEY, started_at time(3))")
        exec_sql(pg_database, "INSERT INTO t VALUES (1, TIME '13:45:07.9995')")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match, result.row_diffs
        assert any(w.rule is NormalisationRule.TS_2 for w in result.warnings)

    def test_time_precision_pair_reports_changed_when_rounded_values_still_differ(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, started_at time(6))")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, TIME '13:45:07.100000')")
        exec_sql(pg_database, "CREATE TABLE t (id bigint PRIMARY KEY, started_at time(3))")
        exec_sql(pg_database, "INSERT INTO t VALUES (1, TIME '13:45:07.200')")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert not result.is_match
        assert result.changed == 1
        _, _, rule = result.row_diffs[0].changes["started_at"]
        assert rule is NormalisationRule.TS_2


class TestDecimalFixtures:
    def test_pair_compares_at_min_scale_of_the_two_sides(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, total numeric(10,2))")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, 12.50)")
        exec_sql(pg_database, "CREATE TABLE t (id bigint PRIMARY KEY, total numeric(10,1))")
        exec_sql(pg_database, "INSERT INTO t VALUES (1, 12.5)")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match, result.row_diffs
        dec1 = [w for w in result.warnings if w.rule is NormalisationRule.DEC_1]
        assert len(dec1) == 1 and "scale" in dec1[0].message

    def test_negative_zero_renders_as_zero(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v numeric(10,2))")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (-0.00)")
        options = NormaliseOptions(scale_overrides={"v": 2})
        assert render(pg_database, "t", options=options) == ["0.00"]

    def test_round_half_even_at_exact_midpoints_not_round_half_away_from_zero(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v numeric(10,4))")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (0.125), (0.375), (-0.125)")
        options = NormaliseOptions(scale_overrides={"v": 2})
        # 0.125 -> 0.12 (12 is even); 0.375 -> 0.38 (37 is odd, bump to 38);
        # -0.125 -> -0.12 (-13 is odd, bump towards -12). Postgres's own
        # round() gives 0.13/0.38/-0.13 here (away from zero) — this test
        # is exactly what catches that regression.
        assert render(pg_database, "t", options=options) == ["0.12", "0.38", "-0.12"]


class TestFloatFixtures:
    # Postgres's to_char(...'EEEE') renders a lower-case "e" (confirmed
    # empirically) — spec §6.1 only requires "scientific notation", no
    # particular case, so this isn't worth forcing upper-case for.
    _SCI = re.compile(r"^-?\d\.\d{14}e[+-]\d+$")

    def test_default_15_significant_figures_scientific_notation(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v float8)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (0.1 + 0.2), (1e308)")
        rows = render(pg_database, "t")
        for r in rows:
            assert self._SCI.match(r), r

    def test_negative_zero_and_positive_zero_render_identically(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v float8)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (-0.0), (0.0)")
        rows = render(pg_database, "t")
        assert rows[0] == rows[1]

    def test_nan_both_sides_nan_is_equal(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, v float8)")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, 'NaN')")
        exec_sql(pg_database, "CREATE TABLE t (LIKE s INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO t SELECT * FROM s")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match

    def test_infinity_renders_literally(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v float8)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('Infinity'), ('-Infinity')")
        assert render(pg_database, "t") == ["Infinity", "-Infinity"]

    def test_float_precision_override_changes_significant_digit_count(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v float8)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (1.23456789)")
        rows = render(pg_database, "t", options=NormaliseOptions(float_precision=5))
        assert rows == ["1.2346e+00"]


class TestStringFixtures:
    def test_str1_no_trim_no_case_fold_by_default(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('abc '), ('abc')")
        assert render(pg_database, "t") == ["abc ", "abc"]

    def test_str2_trim_makes_trailing_whitespace_variant_match(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, 'abc ')")
        exec_sql(pg_database, "CREATE TABLE t (LIKE s INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO t VALUES (1, 'abc')")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        without_trim = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert not without_trim.is_match

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        with_trim = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"], trim=True)
        assert with_trim.is_match

    def test_strasse_vs_strasse_differ_even_case_insensitive(self, pg_database):
        # spec §6.4 explicitly calls out documenting this: lower() doesn't
        # fold the German ß -> "ss" expansion, so these stay unequal even
        # with --case-insensitive on.
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('Straße'), ('STRASSE')")
        rows = render(pg_database, "t", options=NormaliseOptions(case_insensitive=True))
        assert rows[0] != rows[1]

    def test_emoji_rtl_and_10000_char_string_pass_through_unchanged(self, pg_database):
        conn = connector_for(pg_database)
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text)")
        long_str = "y" * 10_000
        values = ["😀🎉", "اختبار النص", long_str]
        for v in values:
            exec_sql(pg_database, f"INSERT INTO t (v) VALUES ({conn.quote_literal(v)})")
        assert render(pg_database, "t") == values

    def test_nul_byte_cannot_be_stored_in_postgres_text(self, pg_database):
        # Documented Postgres limitation, not a rowproof one: the server
        # itself refuses an embedded NUL in text (0x00 isn't representable
        # in its on-disk text format), so there's no normalise_expr
        # behaviour to prove here beyond "this never reaches SQL".
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text)")
        with pytest.raises(Exception):
            exec_sql(pg_database, "INSERT INTO t (v) VALUES (E'a\\x00b')")


class TestNullFixtures:
    def test_null_vs_empty_string_vs_literal_null_text_all_distinct(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (NULL), (''), ('NULL')")
        rows = render(pg_database, "t")
        assert rows == ["\\N", "", "NULL"]
        assert len(set(rows)) == 3

    def test_null_vs_zero_int(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v integer)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (NULL), (0)")
        # NULL-1's canonical string ("\N") and INT-1's ("0") are both
        # genuinely distinct in SQL. Both come back as plain Python str:
        # INT-1's normalise_expr wraps the column in `::text` in the SQL
        # itself (postgres.py), so a real driver (psycopg) hands back the
        # string "0", not the int 0 — confirmed against a real server;
        # only the old psql-subprocess stand-in's _smart_cast helper
        # guessed digit strings back into Python ints, and only because it
        # had no column-type metadata to know better.
        rows = render(pg_database, "t")
        assert rows[0] == "\\N"
        assert rows[1] == "0"
        assert rows[0] != rows[1]


class TestBooleanFixtures:
    def test_true_false_render_as_literal_words(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v boolean)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (true), (false)")
        assert render(pg_database, "t") == ["true", "false"]

    def test_boolean_vs_integer_0_1_is_excluded_as_incompatible_not_silently_mapped(self, pg_database):
        # spec §6.1's Notes column says boolean-vs-int should "map, cite
        # rule" — that's a cross-representation coercion (relevant once a
        # second engine, e.g. ClickHouse's UInt8-as-bool, is in play from
        # M2 on). Coercing *any* Postgres integer column into "maybe a
        # boolean" within a single engine is a real footgun (it would
        # silently start treating unrelated int columns as compatible
        # with boolean ones), so M1 deliberately does NOT guess: a
        # boolean column paired with an integer column is reported as an
        # incompatible-family exclusion rather than silently mapped. This
        # is a documented scope limit, not an oversight.
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, v boolean)")
        exec_sql(pg_database, "CREATE TABLE t (id bigint PRIMARY KEY, v integer)")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        src_cols = {c.name: c for c in src.get_schema(table_ref("db", "s"))}
        tgt_cols = {c.name: c for c in tgt.get_schema(table_ref("db", "t"))}
        m = match_columns(src_cols, tgt_cols, ["v"])
        assert m.matched == []
        assert m.excluded == ["v"]


class TestUuidFixtures:
    def test_uuid_renders_lower_case_hyphenated_regardless_of_input_case(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v uuid)")
        exec_sql(
            pg_database,
            "INSERT INTO t (v) VALUES "
            "('A1B2C3D4-E5F6-4789-A012-B3C4D5E6F789'), "
            "('a1b2c3d4-e5f6-4789-a012-b3c4d5e6f789')",
        )
        rows = render(pg_database, "t")
        assert rows[0] == rows[1] == "a1b2c3d4-e5f6-4789-a012-b3c4d5e6f789"

    def test_uuid_without_hyphens_input_still_renders_hyphenated(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v uuid)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('a1b2c3d4e5f64789a012b3c4d5e6f789')")
        assert render(pg_database, "t") == ["a1b2c3d4-e5f6-4789-a012-b3c4d5e6f789"]


class TestArrayFixtures:
    def test_int_array_renders_bracket_format_order_sensitive(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v integer[])")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('{1,2,3}')")
        assert render(pg_database, "t") == ["[1,2,3]"]

    def test_empty_array_vs_null_stay_distinct(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v integer[])")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('{}'), (NULL)")
        assert render(pg_database, "t") == ["[]", "\\N"]

    def test_text_array_with_null_element(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v text[])")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES (ARRAY['a', NULL, 'c'])")
        assert render(pg_database, "t") == ["[a,\\N,c]"]


class TestJsonFixtures:
    def test_jsonb_key_order_is_engine_side_canonicalised(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v jsonb)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('{\"b\":2,\"a\":1}'), ('{\"a\": 1, \"b\": 2}')")
        rows = render(pg_database, "t")
        assert rows[0] == rows[1]

    def test_plain_json_also_canonicalised_via_jsonb_cast(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v json)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('{\"b\": 2,   \"a\":1}'), ('{\"a\":1,\"b\":2}')")
        rows = render(pg_database, "t")
        assert rows[0] == rows[1]


class TestEnumFixtures:
    def test_enum_renders_as_label_text(self, pg_database):
        exec_sql(pg_database, "CREATE TYPE order_status AS ENUM ('pending', 'shipped', 'delivered')")
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v order_status)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('shipped')")
        assert render(pg_database, "t") == ["shipped"]


class TestBinaryFixtures:
    def test_bytea_renders_lower_case_hex_without_backslash_x_prefix(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v bytea)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('\\xDEADBEEF')")
        assert render(pg_database, "t") == ["deadbeef"]


class TestDateTimeFixtures:
    def test_date_renders_iso(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v date)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('2024-03-01')")
        assert render(pg_database, "t") == ["2024-03-01"]

    def test_time_renders_with_six_fractional_digits(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE t (id serial PRIMARY KEY, v time)")
        exec_sql(pg_database, "INSERT INTO t (v) VALUES ('13:45:07.5')")
        assert render(pg_database, "t") == ["13:45:07.500000"]


class TestUnknownTypeNeverFailsARun:
    def test_unknown_type_falls_back_to_text_cast_and_warns_once(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE s (id bigint PRIMARY KEY, v point)")
        exec_sql(pg_database, "INSERT INTO s VALUES (1, '(1,2)')")
        exec_sql(pg_database, "CREATE TABLE t (LIKE s INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO t SELECT * FROM s")
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "s"), table_ref("db", "t"), key_columns=["id"])
        assert result.is_match
        unk = [w for w in result.warnings if w.rule is NormalisationRule.UNK_1]
        assert len(unk) == 1
