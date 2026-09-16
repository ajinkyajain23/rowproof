"""Joindiff (spec §4.2) against real Postgres — same database, one FULL
OUTER JOIN query. Complements tests/unit/test_joindiff_core.py (which
proves the classification logic against sqlite); this proves the actual
generated SQL (FULL OUTER JOIN, IS DISTINCT FROM, subquery aliasing) is
valid Postgres and gives the same right answer spec §13 requires: "verify
on real databases before calling a milestone done."
"""

from __future__ import annotations

from rowproof.core.joindiff import diff as joindiff
from rowproof.core.models import Algorithm, NormalisationRule

from .conftest import exec_sql
from .test_m0_acceptance import connector_for, table_ref


class TestJoindiffAgainstRealPostgres:
    def test_identical_tables_match(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, amount numeric(10,2))")
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT g, 'row-' || g, g * 1.5 FROM generate_series(1, 5000) g",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        conn = connector_for(pg_database)
        result = joindiff(conn, conn, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.is_match, result.row_diffs
        assert result.algorithm is Algorithm.JOINDIFF
        assert result.source_count == result.target_count == 5000

    def test_missing_extra_and_changed_rows_in_a_single_query(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 1000) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(
            pg_database,
            "INSERT INTO b SELECT g, CASE WHEN g = 500 THEN 'CHANGED' ELSE 'row-' || g END "
            "FROM generate_series(2, 1001) g",  # id=1 missing, id=1001 extra, id=500 changed
        )

        conn = connector_for(pg_database)
        result = joindiff(conn, conn, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert not result.is_match
        assert result.missing_in_target == 1
        assert result.extra_in_target == 1
        assert result.changed == 1
        by_kind = {rd.kind: rd for rd in result.row_diffs}
        assert by_kind["missing"].key == (1,)
        assert by_kind["extra"].key == (1001,)
        assert by_kind["changed"].key == (500,)

    def test_dec_1_pair_scale_applies_inside_the_join(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, total numeric(10,2))")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 12.50)")
        exec_sql(pg_database, "CREATE TABLE b (id bigint PRIMARY KEY, total numeric(10,1))")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, 12.5)")

        conn = connector_for(pg_database)
        result = joindiff(conn, conn, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.is_match, result.row_diffs
        assert any(w.rule is NormalisationRule.DEC_1 for w in result.warnings)

    def test_max_diff_rows_bounds_the_detail_fetch_with_exact_counts(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'v-' || g FROM generate_series(1, 2000) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'CHANGED-' || g FROM generate_series(1, 2000) g")

        conn = connector_for(pg_database)
        result = joindiff(
            conn, conn, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], max_diff_rows=25,
        )

        assert result.changed == 2000  # exact count, not capped
        assert len(result.row_diffs) == 25  # detail list IS capped
        assert result.truncated

    def test_where_filters_both_sides_inside_the_join(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, status text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, CASE WHEN g % 2 = 0 THEN 'active' ELSE 'closed' END FROM generate_series(1, 200) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        conn = connector_for(pg_database)
        result = joindiff(
            conn, conn, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], where="status = 'active'",
        )
        assert result.is_match
        assert result.source_count == result.target_count == 100
