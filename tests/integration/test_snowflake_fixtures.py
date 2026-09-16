"""SPEC §6.4's fixture list, built as one Postgres table and one Snowflake
table covering every canonical-form category it names -- timestamps
(tz-aware), times, dates, decimals (incl. -0.00), floats (incl. NaN/
Infinity/-0.0 edge cases), strings (incl. unicode), nulls, booleans,
uuids, and arrays -- diffed cross-engine. Same two-test shape as
test_clickhouse_real.py's own `TestSpec64FixturesAcrossEngines` (every
value equal -> MATCH; every comparable value changed -> each column
individually reported with the right rule), which explicitly deferred
this file's scope to "M3 (Snowflake) hasn't started" -- this is that
work.

One difference from the Postgres/ClickHouse version: a bare `tm` (TIME)
column is included and actually compared here, not excluded -- unlike
ClickHouse, Snowflake has a native TIME type, so there's no
family-incompatibility case to prove for it in this file.
"""

from __future__ import annotations

import pytest

from rowproof.connectors._sfwire import run_query
from rowproof.connectors.postgres import PostgresConnector
from rowproof.connectors.snowflake import SnowflakeConnector
from rowproof.core.hashdiff import diff as hashdiff
from rowproof.core.models import TableRef

from .conftest import SF_DSN_URL, _sf_dsn, exec_sql

pytestmark = pytest.mark.usefixtures("_require_snowflake")


class TestSpec64FixturesAcrossEnginesSnowflake:
    _PG_DDL = """
        CREATE TABLE a (
            id bigint PRIMARY KEY,
            ts_tz timestamptz(6), tm time(6), dt date,
            amount numeric(12,2), amount_negzero numeric(12,2),
            score float8, score_big float8, score_negzero float8, score_nan float8, score_inf float8,
            name text, name_unicode text, flag boolean, uid uuid, tags integer[], note text
        )
    """
    _SF_DDL = """
        CREATE TABLE "{database}"."{schema}"."b" (
            "id" NUMBER(38,0),
            "ts_tz" TIMESTAMP_TZ(6), "tm" TIME(6), "dt" DATE,
            "amount" NUMBER(12,2), "amount_negzero" NUMBER(12,2),
            "score" FLOAT, "score_big" FLOAT, "score_negzero" FLOAT, "score_nan" FLOAT, "score_inf" FLOAT,
            "name" TEXT, "name_unicode" TEXT, "flag" BOOLEAN, "uid" VARCHAR(36), "tags" ARRAY, "note" TEXT
        )
    """

    def _build(self, pg_database, sf_schema, pg_row, sf_row):
        exec_sql(pg_database, self._PG_DDL)
        exec_sql(pg_database, f"INSERT INTO a VALUES {pg_row}")
        dsn = _sf_dsn()
        run_query(dsn, self._SF_DDL.format(database=dsn.database, schema=sf_schema))
        run_query(dsn, f'INSERT INTO "{dsn.database}"."{sf_schema}"."b" SELECT {sf_row}')

    def _diff(self, pg_database, sf_schema):
        source = PostgresConnector()
        source.connect(pg_database)
        target = SnowflakeConnector()
        target.connect(SF_DSN_URL)
        return hashdiff(
            source, target,
            TableRef(engine="postgres", database=pg_database, table="a"),
            TableRef(engine="snowflake", database=_sf_dsn().database, table="b", schema=sf_schema),
            key_columns=["id"], max_diff_rows=100,
        )

    def test_every_fixture_value_matches_across_engines(self, pg_database, sf_schema):
        pg_row = (
            "(1, '2024-03-01 10:00:00+05:30', '13:45:07.123456', '2024-06-15', "
            "12.50, -0.00, 0.1::float8 + 0.2::float8, 1e308, -0.0, 'NaN', 'Infinity', "
            "'hello world', 'café ☃ 日本語', true, "
            "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', ARRAY[1,2,3], NULL)"
        )
        sf_row = (
            "1, '2024-03-01 10:00:00+05:30'::timestamp_tz, '13:45:07.123456'::time, '2024-06-15'::date, "
            "12.50, -0.00, 0.1::float + 0.2::float, 1e308::float, -0.0::float, 'NaN'::float, 'Infinity'::float, "
            "'hello world', 'café ☃ 日本語', true, "
            "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', ARRAY_CONSTRUCT(1,2,3), NULL"
        )
        self._build(pg_database, sf_schema, pg_row, sf_row)
        result = self._diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_one_value_changed_per_column_is_individually_reported(self, pg_database, sf_schema):
        pg_row = (
            "(1, '2024-03-01 10:00:00+05:30', '13:45:07.123456', '2024-06-15', "
            "12.50, -0.00, 0.1::float8 + 0.2::float8, 1e308, -0.0, 'NaN', 'Infinity', "
            "'hello world', 'café ☃ 日本語', true, "
            "'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11', ARRAY[1,2,3], NULL)"
        )
        # Every comparable column changed. score_nan is deliberately left
        # alone (still NaN on both sides): spec's "NaN (both sides NaN ->
        # equal)" is a MATCH rule, not something to invert here -- proven
        # by its own absence from `changes` below, not a positive assertion.
        sf_row = (
            "1, '2024-03-01 10:00:01+05:30'::timestamp_tz, '13:45:08.123456'::time, '2024-06-16'::date, "
            "12.51, 0.01, 0.5::float, 9e307::float, 1.0::float, 'NaN'::float, '-Infinity'::float, "
            "'hello there', 'café ☃ 日本', false, "
            "'B0EEBC999C0B4EF8BB6D6BB9BD380A22', ARRAY_CONSTRUCT(1,2,4), 'not null anymore'"
        )
        self._build(pg_database, sf_schema, pg_row, sf_row)
        result = self._diff(pg_database, sf_schema)

        assert not result.is_match
        assert result.changed == 1
        changes = result.row_diffs[0].changes
        expected_rules = {
            "ts_tz": "TS-1", "tm": "TIME-1", "dt": "DATE-1",
            "amount": "DEC-1", "amount_negzero": "DEC-1",
            "score": "FLT-1", "score_big": "FLT-1", "score_negzero": "FLT-1", "score_inf": "FLT-1",
            "name": "STR-1", "name_unicode": "STR-1", "note": "STR-1",
            "flag": "BOOL-1", "uid": "UUID-1",
        }
        for col, expected_rule in expected_rules.items():
            assert col in changes, f"{col} missing from changes: {set(changes)}"
            _, _, rule = changes[col]
            assert rule.value == expected_rule, f"{col}: expected {expected_rule}, got {rule}"
        assert "score_nan" not in changes
