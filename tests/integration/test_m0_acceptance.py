"""M0 acceptance criteria (spec §13 M0), verified against a real Postgres
instance per spec's instruction: "Verify on real databases before calling
a milestone done." Runs against the Postgres 16 container docker-compose.yml
starts (see docs/LAPTOP_SETUP.md).

Each test method's docstring/name is the literal acceptance-criterion
bullet it proves.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from rowproof.cli.main import main as cli_main
from rowproof.connectors import _pgwire
from rowproof.connectors.postgres import PostgresConnector
from rowproof.core.hashdiff import diff
from rowproof.core.models import TableRef

from .conftest import ADMIN_DSN_URL, exec_sql


def table_ref(db: str, table: str) -> TableRef:
    return TableRef(engine="postgres", database=db, schema="public", table=table)


def connector_for(dsn_url: str) -> PostgresConnector:
    c = PostgresConnector()
    c.connect(dsn_url)
    return c


class TestCliExitCodesAgainstRealPostgres:
    """The other tests below mostly call core.hashdiff.diff() directly so
    they can time the algorithm precisely and inspect DiffResult objects.
    That leaves a real gap: it never proves the `rowproof` CLI *itself*
    returns the right process exit code for the ordinary (non-error) match
    and differences cases — only DiffResult.exit_code() was checked there,
    and only the error paths (exit 2) were previously driven through
    cli_main(). These two tests close that gap by going through the actual
    CLI entry point end to end, against real Postgres."""

    def test_cli_diff_returns_exit_0_when_tables_match(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 500) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 0
        assert "MATCH" in captured.out

    def test_cli_diff_returns_exit_1_when_tables_differ(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 500) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a WHERE id != 42")  # one missing row

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 1
        assert "DIFFERENT" in captured.out


class TestIdenticalOneMillionRows:
    def test_two_identical_1m_row_tables_match_exit_0_under_10s(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, updated_at timestamptz)")
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT g, 'row-' || g, TIMESTAMPTZ '2024-01-01' "
            "FROM generate_series(1, 1000000) g",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        src = connector_for(pg_database)
        tgt = connector_for(pg_database)
        ref_a, ref_b = table_ref("db", "a"), table_ref("db", "b")

        start = time.monotonic()
        result = diff(src, tgt, ref_a, ref_b, key_columns=["id"])
        elapsed = time.monotonic() - start

        assert result.is_match
        assert result.exit_code() == 0
        assert result.source_count == 1_000_000
        assert result.target_count == 1_000_000
        assert elapsed < 10.0, f"diff took {elapsed:.1f}s, spec requires under 10s"


class TestDeleteUpdateInsert:
    def test_delete_10_update_10_insert_10_reported_and_classified(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 1000) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        # delete 10 rows from target
        exec_sql(pg_database, "DELETE FROM b WHERE id BETWEEN 1 AND 10")
        # update 10 rows in target
        exec_sql(pg_database, "UPDATE b SET name = name || '-CHANGED' WHERE id BETWEEN 11 AND 20")
        # insert 10 new rows into target only
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'row-' || g FROM generate_series(1001, 1010) g")

        src = connector_for(pg_database)
        tgt = connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"], row_threshold=50)

        assert result.missing_in_target == 10
        assert result.extra_in_target == 10
        assert result.changed == 10
        assert not result.is_match
        assert result.exit_code() == 1

        missing = {rd.key[0] for rd in result.row_diffs if rd.kind == "missing"}
        extra = {rd.key[0] for rd in result.row_diffs if rd.kind == "extra"}
        changed = {rd.key[0] for rd in result.row_diffs if rd.kind == "changed"}
        assert missing == set(range(1, 11))
        assert extra == set(range(1001, 1011))
        assert changed == set(range(11, 21))


class TestEmptyTableEdgeCases:
    """Not a numbered M0 bullet, but a real untested edge case: what
    happens when one or both sides have zero rows (a MIN/MAX over an empty
    table is SQL NULL, which is exactly the kind of thing that breaks
    range-based segmentation if not handled deliberately)."""

    def test_source_empty_target_has_rows_reports_all_as_extra(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'row-' || g FROM generate_series(1, 25) g")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.source_count == 0
        assert result.target_count == 25
        assert result.extra_in_target == 25
        assert result.missing_in_target == 0
        assert not result.is_match

    def test_target_empty_source_has_rows_reports_all_as_missing(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 25) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.source_count == 25
        assert result.target_count == 0
        assert result.missing_in_target == 25
        assert result.extra_in_target == 0
        assert not result.is_match

    def test_both_sides_empty_is_a_match(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.is_match
        assert result.exit_code() == 0


class TestColumnsAndExclude:
    """Also untested until now: --columns and --exclude (spec §7) both
    thread through _build_plan already, but nothing exercised them."""

    def test_exclude_hides_a_differing_column_from_the_diff(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, noisy text)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g, 'A' FROM generate_series(1, 10) g")
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'row-' || g, 'B' FROM generate_series(1, 10) g")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        excluded = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"], exclude=["noisy"])
        assert excluded.is_match

        src2, tgt2 = connector_for(pg_database), connector_for(pg_database)
        not_excluded = diff(src2, tgt2, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])
        assert not not_excluded.is_match
        assert not_excluded.changed == 10

    def test_columns_restricts_comparison_to_the_named_set(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, noisy text)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g, 'A' FROM generate_series(1, 10) g")
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'row-' || g, 'B' FROM generate_series(1, 10) g")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], columns=["id", "name"],
        )
        assert result.is_match


class TestWhereFilteringReal:
    """--where / --where-source / --where-target (spec §7), verified against
    real Postgres: the FakeConnector unit tests
    (tests/unit/test_hashdiff_core.py::TestWhereFiltering) prove the SQL is
    *built* correctly; these prove Postgres actually executes it the way we
    expect, including the duplicate-key-scoping subtlety."""

    def test_where_excludes_rows_on_both_sides_real_postgres(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 20) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")
        # tamper with a row that --where will exclude from the comparison
        exec_sql(pg_database, "UPDATE b SET name = 'TAMPERED' WHERE id = 1")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], where="id > 5",
        )
        assert result.is_match
        assert result.source_count == 15
        assert result.target_count == 15

    def test_where_source_and_where_target_differ_per_side_real_postgres(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 10) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], where_source="id > 5", where_target="id > 5",
        )
        assert result.is_match
        assert result.source_count == 5
        assert result.target_count == 5

    def test_duplicate_key_scoped_by_where_real_postgres(self, pg_database):
        # No PK (duplicates allowed at the table level); status='archived'
        # rows hold a duplicate id that only matters if --where lets it in.
        exec_sql(pg_database, "CREATE TABLE a (id bigint, status text, name text)")
        exec_sql(
            pg_database,
            "INSERT INTO a VALUES (1, 'active', 'a'), (1, 'archived', 'a-dup'), (2, 'active', 'b')",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        with pytest.raises(Exception) as exc_info:
            diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])
        assert "NonUniqueKeyError" in type(exc_info.value).__name__

        src2, tgt2 = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src2, tgt2, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], where="status = 'active'",
        )
        assert result.is_match
        assert result.source_count == 2

    def test_cli_where_flag_end_to_end(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 20) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")
        exec_sql(pg_database, "UPDATE b SET name = 'TAMPERED' WHERE id = 1")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--where", "id > 5",
        ]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 0
        assert "MATCH" in captured.out


class TestMixedCaseTextKeysReal:
    """Regression test for a real correctness bug: segment boundaries for a
    non-numeric key used to be computed by sorting a sample *in Python*
    (byte/codepoint order) but then applied via a plain `WHERE key >= lo`
    comparison that used the *column's own collation*. Those two orderings
    only coincide by luck. This sandbox's default database collation
    happens to be "C" (byte order) already, so it can't expose the bug on
    its own -- this test declares the key column COLLATE "en-x-icu"
    (Postgres's ICU English collation, a real locale-aware, roughly
    case-insensitive ordering) to reproduce what a normal en_US-collated
    production database looks like, and prove the fix (comparing and
    sorting every non-numeric key under a forced COLLATE "C" throughout)
    is what makes this correct rather than incidental."""

    def test_mixed_case_text_keys_diff_correctly_under_locale_aware_collation(self, pg_database):
        exec_sql(pg_database, 'CREATE TABLE a (id text COLLATE "en-x-icu" PRIMARY KEY, val text)')
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")

        # Mixed upper/lower leading characters on purpose: under a
        # locale-aware collation these interleave in roughly alphabetical
        # (case-blind) order; under plain byte order, every capital letter
        # sorts before every lowercase one. The two orderings disagree
        # throughout the whole range, not just at the ends.
        keys = []
        for i in range(4000):
            letter = chr(ord("A") + (i % 26))
            letter = letter if i % 2 == 0 else letter.lower()
            keys.append(f"{letter}{i:05d}")

        rows_a = [(k, "v") for k in keys]

        def insert_rows(table: str, rows: list[tuple[str, str]]) -> None:
            # Batch to stay well under a single psql -c command's practical
            # size; 500 rows per statement is plenty for 4000 total.
            for start in range(0, len(rows), 500):
                chunk = rows[start : start + 500]
                literal = ", ".join("(" + "'" + k.replace("'", "''") + "', '" + v + "')" for k, v in chunk)
                exec_sql(pg_database, f"INSERT INTO {table} (id, val) VALUES {literal}")

        insert_rows("a", rows_a)
        insert_rows("b", rows_a)

        # Scatter changes across the range (not just at the min/max) so
        # this specifically exercises mid-range segment/bisection boundary
        # handling, not the separate "true min" bug covered elsewhere.
        changed_keys = {keys[7], keys[1500], keys[2001], keys[3333], keys[3999]}
        for k in changed_keys:
            exec_sql(pg_database, f"UPDATE b SET val = 'CHANGED' WHERE id = '{k}'")

        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], row_threshold=50,
        )

        assert not result.is_match
        assert result.changed == len(changed_keys)
        found_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "changed"}
        assert found_keys == changed_keys


class TestCompositeKeyReal:
    def test_composite_key_tenant_id_order_id(self, pg_database):
        exec_sql(
            pg_database,
            "CREATE TABLE orders (tenant_id bigint, order_id bigint, status text, "
            "PRIMARY KEY (tenant_id, order_id))",
        )
        exec_sql(
            pg_database,
            "INSERT INTO orders SELECT t, o, 'open' FROM generate_series(1,5) t, generate_series(1,50) o",
        )
        exec_sql(pg_database, "CREATE TABLE orders_b (LIKE orders INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO orders_b SELECT * FROM orders")
        exec_sql(pg_database, "UPDATE orders_b SET status = 'closed' WHERE tenant_id = 3 AND order_id = 25")

        src = connector_for(pg_database)
        tgt = connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "orders"), table_ref("db", "orders_b"),
            key_columns=["tenant_id", "order_id"], row_threshold=20,
        )

        assert result.changed == 1
        assert result.row_diffs[0].key == (3, 25)


class TestTextColumnsWithLeadingZerosAreNotCorrupted:
    """Not one of the spec's numbered M0 acceptance bullets, but a real bug
    found by inspection: the psql-subprocess stand-in (_pgwire.py) gets
    every value back as plain text and guesses which ones are really
    integers so core/segmentation.py's arithmetic gets real Python ints.
    That guess used to be "all-digits => int", which silently corrupts a
    *text* column whose value happens to look numeric with a leading zero
    (zip codes, external IDs, phone extensions) by dropping the leading
    zero — e.g. '02139' silently became the int 2139. This wouldn't happen
    with a real driver (psycopg knows the column's actual type), so it's
    specific to this environment's stand-in; the fix narrows the guess to
    match spec §6.1's own INT-1 definition ("no leading zeros"), which
    closes the leading-zero case but can't fully close the underlying
    ambiguity — a bare text value that looks like a clean integer (e.g. a
    text column containing exactly '42') is still indistinguishable from a
    real integer over a text-only protocol. That residual gap goes away
    entirely once this is ported to real psycopg (see DEV_ENVIRONMENT.md)."""

    def test_leading_zero_text_value_is_reported_intact_not_as_a_corrupted_int(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, zip_code text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '02139'), (2, '94105')")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, '02140'), (2, '94105')")  # id=1 changed

        src = connector_for(pg_database)
        tgt = connector_for(pg_database)
        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.changed == 1
        changed = result.row_diffs[0]
        source_value, target_value = changed.changes["zip_code"][0], changed.changes["zip_code"][1]
        assert (source_value, target_value) == ("02139", "02140"), (
            f"leading zero was corrupted: got {(source_value, target_value)!r} "
            "instead of ('02139', '02140')"
        )
        assert isinstance(source_value, str) and isinstance(target_value, str)


class TestUuidKeySegmentBalance:
    def test_uuid_key_no_segment_holds_more_than_2x_average(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id uuid PRIMARY KEY, name text)")
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT gen_random_uuid(), 'x' FROM generate_series(1, 5000)",
        )
    # gen_random_uuid() needs pgcrypto on older PG; Postgres 16 has it
    # built into core (no extension needed) via gen_random_uuid().

        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")  # identical: isolates segmentation, not correctness

        src = connector_for(pg_database)
        tgt = connector_for(pg_database)

        captured_counts = []
        real_query = src.query

        def spy(sql, params=None):
            rows = real_query(sql, params)
            if sql.startswith("SELECT COUNT(*), ("):
                captured_counts.append(rows[0][0])
            return rows

        src.query = spy  # type: ignore[method-assign]

        result = diff(src, tgt, table_ref("db", "a"), table_ref("db", "b"), key_columns=["id"])

        assert result.is_match
        assert len(captured_counts) == result.segments_examined
        total = sum(captured_counts)
        avg = total / len(captured_counts)
        assert max(captured_counts) <= 2 * avg, captured_counts


class TestSegmentQueriesUsePkIndex:
    """Perf regression: segment stats/row-fetch queries used to wrap
    non-numeric keys in an expression (CAST(...AS TEXT), or a forced
    COLLATE that didn't match the column's own default) that made
    Postgres unable to use the PK index for the range predicate at all --
    every segment query degraded to a Seq Scan regardless of table size,
    defeating the entire point of segmentation. Confirmed via EXPLAIN
    against real tables built the same way a normal migration would (no
    special collation declared), for both a uuid key and a text key."""

    def _segment_plan(self, dsn_url: str, table: str, key: str) -> str:
        from rowproof.core.hashdiff import _build_plan, _get_bounds, _initial_segments, _segment_stats_sql

        conn = connector_for(dsn_url)
        ref = table_ref("db", table)
        plan = _build_plan(conn, ref, [key], None, None)
        lo, hi, count = _get_bounds(plan)
        segments = _initial_segments(plan, count, lo, hi)
        # A bounded (non-open-ended) middle segment is the realistic case:
        # a narrow slice of the key range, exactly what an index range
        # scan is supposed to be good at.
        segment = next(s for s in segments if s.hi is not None)
        sql = _segment_stats_sql(plan, segment)
        rows = conn.query(f"EXPLAIN {sql}")
        return "\n".join(r[0] for r in rows)

    def test_uuid_key_segment_query_uses_pk_index(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id uuid PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT gen_random_uuid(), 'row' FROM generate_series(1, 50000)")
        exec_sql(pg_database, "ANALYZE a")

        plan_text = self._segment_plan(pg_database, "a", "id")
        assert "Seq Scan" not in plan_text, plan_text
        assert "Index" in plan_text, plan_text

    def test_text_key_segment_query_uses_pk_index_under_default_collation(self, pg_database):
        # Explicit COLLATE "C" (rather than trusting whatever locale the
        # server happens to default to): this project's own dev sandbox
        # defaulted to "C.UTF-8", but this project's docker-compose.yml
        # Postgres 16 image defaults to "en_US.utf8" — a locale-aware
        # collation _key_expr_for_ordering (core/hashdiff.py) correctly
        # treats as NOT byte-order-safe, forcing a COLLATE "C" range
        # predicate that can't use an index built under en_US.utf8 (a real
        # correctness trade-off, not a bug — see that function's own
        # docstring). This test is specifically about the "collation IS
        # byte-order-safe, so the index IS usable" branch, which needs an
        # explicit "C" collation to be deterministic across environments.
        exec_sql(pg_database, 'CREATE TABLE a (id text COLLATE "C" PRIMARY KEY, name text)')
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT 'k' || lpad(g::text, 8, '0'), 'row' FROM generate_series(1, 50000) g",
        )
        exec_sql(pg_database, "ANALYZE a")

        plan_text = self._segment_plan(pg_database, "a", "id")
        assert "Seq Scan" not in plan_text, plan_text
        assert "Index" in plan_text, plan_text


class TestNoPrimaryKeyExitsTwo:
    def test_table_without_pk_and_no_key_flag_exits_2_with_clear_message(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (name text)")
        exec_sql(pg_database, "CREATE TABLE b (name text)")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 2
        assert "primary key" in captured.err.lower() or "--key" in captured.err

    def test_still_clean_exit_2_no_traceback_when_verbose_is_on(self, pg_database, capsys):
        # Regression test for a real bug the network-kill test below found:
        # cmd_diff used to re-raise the caught exception when --verbose was
        # set (meaning to show a traceback for debugging), which instead
        # crashed the process outright — Python's own unhandled-exception
        # handler prints the traceback AND exits with status 1, not the
        # documented 2. Any error path must stay exit-2/no-traceback
        # whether or not --verbose is set; this is the fast, deterministic
        # check, the killed-Postgres test below is the slow, real-world one.
        exec_sql(pg_database, "CREATE TABLE a (name text)")
        exec_sql(pg_database, "CREATE TABLE b (name text)")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--verbose"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 2
        assert "Traceback" not in captured.err


class TestNonUniqueKeyExitsTwo:
    def test_non_unique_key_exits_2_and_shows_duplicate_example(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (email text, name text)")
        exec_sql(
            pg_database,
            "INSERT INTO a VALUES ('a@x.com','A'), ('a@x.com','A2'), ('b@x.com','B')",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "email"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 2
        assert "a@x.com" in captured.err


class TestExplainExecutesNoDataQueries:
    def test_explain_prints_sql_and_touches_no_row_data(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 100) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        captured_sql: list[str] = []
        real_run_query_on = _pgwire.run_query_on

        def spying_run_query_on(conn, sql, timeout=None):
            captured_sql.append(sql)
            return real_run_query_on(conn, sql, timeout)

        import rowproof.connectors._pgwire as pgwire_mod

        # PostgresConnector.query() runs every diff/explain query over one
        # persistent connection via run_query_on() (see _pgwire.py's
        # module docstring) — that's the hot path to spy on, not run_query
        # (the separate one-shot path connect()'s own probes and test
        # admin helpers use, which explain's actual query traffic never
        # touches).
        pgwire_mod.run_query_on = spying_run_query_on
        try:
            argv = ["explain", f"{pg_database}/a", f"{pg_database}/b", "--key", "id"]
            code = cli_main(argv)
        finally:
            pgwire_mod.run_query_on = real_run_query_on

        captured_out = capsys.readouterr()

        assert code == 0
        assert len(captured_out.out.strip()) > 0

        # "Executes nothing" means no query that touches table *rows*: no
        # bounds scan, no per-segment hash, no row-level fetch, and no
        # uniqueness probe. Schema introspection (information_schema) is
        # the one thing explain legitimately still needs, since the SQL it
        # prints has to reflect the table's real column types.
        forbidden_prefixes = ("SELECT MIN(", "SELECT COUNT(*), (")
        for sql in captured_sql:
            assert not sql.startswith(forbidden_prefixes), f"explain executed a data query: {sql}"
            assert "GROUP BY" not in sql, f"explain executed the uniqueness probe: {sql}"
            # information_schema lookups are the schema introspection
            # explain legitimately needs (see comment above). connect()
            # itself no longer issues a "SELECT 1" health-check query —
            # it opens one persistent connection directly and a failed
            # open_connection() raises on its own — but the query traffic
            # spied on here is only ever what explain() itself runs
            # through that connection, so this stays a strict allow-list.
            assert "information_schema" in sql, f"unexpected query during explain: {sql}"


def _wait_for_postgres_ready(attempts: int = 20, delay: float = 0.25) -> None:
    last_error = None
    for _ in range(attempts):
        try:
            _pgwire.check_connection(_pgwire.parse_pg_dsn(ADMIN_DSN_URL))
            return
        except Exception as e:  # noqa: BLE001
            last_error = e
            time.sleep(delay)
    raise RuntimeError(f"Postgres did not come back up after restart: {last_error}")


class TestNetworkDropMidRun:
    def test_postgres_actually_killed_mid_run_is_a_clean_error_no_traceback(self, pg_database):
        """This does NOT simulate a failure — it really stops the Postgres
        service while `rowproof diff` is running against it (a real
        subprocess, not an in-process call), so the CLI has to survive an
        actual dropped connection, not a mocked exception."""
        import subprocess
        import sys

        # Big enough that the diff is still mid-flight (past connect and
        # into the per-segment queries) when we pull the plug a moment in.
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 3000000) g",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = [
            sys.executable, "-u", "-m", "rowproof.cli.main", "diff",
            f"{pg_database}/a", f"{pg_database}/b", "--key", "id", "--verbose",
        ]
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)

        # --verbose logs every generated SQL statement to stderr as it runs
        # (spec §5) — that's our signal that the diff is genuinely underway.
        # Let a handful through (schema lookup, uniqueness check, and into
        # the per-segment loop) before killing the server out from under it.
        consumed_stderr = []
        sql_lines_seen = 0
        killed_cleanly = False
        for line in proc.stderr:
            consumed_stderr.append(line)
            if line.startswith("[sql]"):
                sql_lines_seen += 1
                if sql_lines_seen >= 3:
                    killed_cleanly = True
                    break

        if not killed_cleanly:
            proc.kill()
            proc.wait(timeout=10)
            pytest.fail(
                "rowproof finished before we could kill Postgres mid-run "
                f"(saw {sql_lines_seen} [sql] lines; stderr so far: {''.join(consumed_stderr)!r})"
            )

        # This project's dev environment runs Postgres via
        # docker-compose.yml (docs/LAPTOP_SETUP.md), not a native
        # `service postgresql` — stop/start the container instead. Both
        # achieve the same thing this test needs: the connection really
        # drops out from under a live query, not a mocked exception.
        project_root = Path(__file__).resolve().parents[2]
        stop = subprocess.run(
            ["docker", "compose", "stop", "postgres"], capture_output=True, text=True, cwd=project_root
        )
        assert stop.returncode == 0, f"could not stop postgres for the test: {stop.stderr}"

        try:
            remaining_stdout, remaining_stderr = proc.communicate(timeout=30)
        finally:
            start = subprocess.run(
                ["docker", "compose", "start", "postgres"], capture_output=True, text=True, cwd=project_root
            )
            assert start.returncode == 0, f"could not restart postgres after the test: {start.stderr}"
            _wait_for_postgres_ready()

        full_stderr = "".join(consumed_stderr) + remaining_stderr

        assert proc.returncode == 2, (
            f"expected exit 2 on a real dropped connection, got {proc.returncode}\n"
            f"stdout: {remaining_stdout}\nstderr: {full_stderr}"
        )
        assert "Traceback" not in full_stderr, f"leaked a traceback:\n{full_stderr}"
        assert "error:" in full_stderr.lower()


class TestPasswordNeverLogged:
    def test_password_never_appears_in_any_output_including_verbose(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 50) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        secret = "S3cr3t-Passw0rd!"
        # pg_database already has a real password baked in from the
        # fixture; swap it for a distinctive one we can grep for, and
        # actually set it on the role so the connection still succeeds.
        dsn_with_secret = pg_database.replace("postgres:postgres@", f"postgres:{secret}@")
        exec_sql(pg_database, f"ALTER USER postgres WITH PASSWORD '{secret}'")
        try:
            argv = ["diff", f"{dsn_with_secret}/a", f"{dsn_with_secret}/b", "--key", "id", "--verbose"]
            code = cli_main(argv)
            captured = capsys.readouterr()

            assert code == 0
            assert secret not in captured.out
            assert secret not in captured.err
        finally:
            # Reset using the *new* password (dsn_with_secret) — the role's
            # password really is `secret` right now, so the old DSN can't
            # authenticate to change it back.
            exec_sql(dsn_with_secret, "ALTER USER postgres WITH PASSWORD 'postgres'")


class TestThreadedHashdiffMatchesSequential:
    """spec §7's `--threads N` was defined in the CLI surface but never
    implemented anywhere -- every segment query ran strictly sequentially,
    one at a time. That alone was the dominant cost in a 100M-row/5-minute
    benchmark run (spec §13 M2): ~167,000 sequential queries at tens of
    milliseconds each is tens of minutes of pure serialisation, independent
    of how fast any single query runs. core/hashdiff.py now runs
    independent segments concurrently across a pool of extra connections
    when threads > 1 -- these tests prove the parallel path produces
    EXACTLY the same result as the sequential one, not just "runs faster."
    """

    def _make_pair_with_differences(self, pg_database):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, amount numeric(10,2))")
        exec_sql(
            pg_database,
            "INSERT INTO a SELECT g, 'row-' || g, (g % 1000)::numeric / 10 "
            "FROM generate_series(1, 200000) g",
        )
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")
        # Scatter real differences across the whole key range so multiple
        # segments are genuinely mismatched, not just one.
        exec_sql(pg_database, "DELETE FROM b WHERE id % 4999 = 0")  # missing in target
        exec_sql(pg_database, "UPDATE b SET amount = amount + 1 WHERE id % 3001 = 0")  # changed
        exec_sql(
            pg_database,
            "INSERT INTO b SELECT g, 'extra-' || g, 9.99 FROM generate_series(200001, 200050) g",
        )  # extra in target

    def test_parallel_result_matches_sequential_result_exactly(self, pg_database):
        self._make_pair_with_differences(pg_database)

        seq_src, seq_tgt = connector_for(pg_database), connector_for(pg_database)
        sequential = diff(
            seq_src, seq_tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], max_diff_rows=100_000,
        )

        par_src = connector_for(pg_database)
        par_tgt = connector_for(pg_database)
        src_pool = [connector_for(pg_database) for _ in range(4)]
        tgt_pool = [connector_for(pg_database) for _ in range(4)]
        try:
            parallel = diff(
                par_src, par_tgt, table_ref("db", "a"), table_ref("db", "b"),
                key_columns=["id"], max_diff_rows=100_000,
                threads=4, source_pool=src_pool, target_pool=tgt_pool,
            )
        finally:
            for c in (*src_pool, *tgt_pool):
                c.close()

        assert parallel.source_count == sequential.source_count
        assert parallel.target_count == sequential.target_count
        assert parallel.missing_in_target == sequential.missing_in_target
        assert parallel.extra_in_target == sequential.extra_in_target
        assert parallel.changed == sequential.changed
        assert parallel.truncated == sequential.truncated

        def key_set(result):
            return {(rd.kind, rd.key) for rd in result.row_diffs}

        assert key_set(parallel) == key_set(sequential)

    def test_threads_flag_without_a_pool_falls_back_to_sequential(self, pg_database):
        """threads > 1 with no pool given (e.g. a connector that doesn't
        support it) must not silently break -- it just runs sequentially,
        per diff()'s own docstring."""
        self._make_pair_with_differences(pg_database)
        src, tgt = connector_for(pg_database), connector_for(pg_database)
        result = diff(
            src, tgt, table_ref("db", "a"), table_ref("db", "b"),
            key_columns=["id"], max_diff_rows=100_000, threads=4,
        )
        assert result.missing_in_target > 0
        assert result.changed > 0
        assert result.extra_in_target > 0
