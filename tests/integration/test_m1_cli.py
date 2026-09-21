"""M1 CLI-level acceptance tests against real Postgres: algorithm
auto-selection (spec §4.2), the new normalisation flags (spec §7), and
the expanded JSON output (spec §8.2). Complements test_m0_acceptance.py
(which already covers the base --key/--columns/--exclude/--where CLI
surface) rather than re-proving that surface.
"""

from __future__ import annotations

import json


from rowproof.cli.main import main as cli_main

from .conftest import exec_sql


class TestAlgorithmAutoSelection:
    def test_auto_picks_joindiff_when_both_sides_are_the_same_database(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 100) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id", "--output", "json"]
        code = cli_main(argv)
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        assert payload["algorithm"] == "joindiff"

    def test_auto_picks_hashdiff_when_databases_differ(self, pg_database, capsys):
        # pg_database is one throwaway database; point source and target
        # at two DIFFERENT throwaway databases to force "different
        # connection" — cheapest way here is comparing a table against
        # itself but through a deliberately different-looking DSN would
        # still resolve identically, so instead this proves the negative
        # directly against test_joindiff_real.py's positive: hashdiff
        # stays the default whenever --algorithm isn't forced and a
        # second database is involved (see conftest.pg_database for how
        # a second one is created).
        import uuid
        from .conftest import HOST_PORT_USER_PW, _run_admin

        name2 = "rowproof_test_" + uuid.uuid4().hex[:16]
        _run_admin(f'CREATE DATABASE "{name2}"')
        dsn2 = f"postgres://{HOST_PORT_USER_PW}/{name2}"
        try:
            exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
            exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 50) g")
            exec_sql(dsn2, "CREATE TABLE b (id bigint PRIMARY KEY, name text)")
            exec_sql(dsn2, "INSERT INTO b SELECT g, 'row-' || g FROM generate_series(1, 50) g")

            argv = ["diff", f"{pg_database}/a", f"{dsn2}/b", "--key", "id", "--output", "json"]
            code = cli_main(argv)
            payload = json.loads(capsys.readouterr().out)

            assert code == 0
            assert payload["algorithm"] == "hashdiff"
        finally:
            _run_admin(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name2}' AND pid <> pg_backend_pid()"
            )
            _run_admin(f'DROP DATABASE IF EXISTS "{name2}"')

    def test_algorithm_hashdiff_forces_hashdiff_even_on_the_same_database(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 50) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--output", "json",
        ]
        code = cli_main(argv)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["algorithm"] == "hashdiff"


class TestNormalisationFlagsEndToEnd:
    def test_trim_flag_makes_trailing_whitespace_match(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'abc ')")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, 'abc')")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id"]
        assert cli_main(argv) == 1
        capsys.readouterr()

        argv_trim = [*argv, "--trim"]
        assert cli_main(argv_trim) == 0

    def test_column_map_flag_matches_differently_named_columns(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, full_name text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'Ada Lovelace')")
        exec_sql(pg_database, "CREATE TABLE b (id bigint PRIMARY KEY, customer_name text)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, 'Ada Lovelace')")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--column-map", "full_name:customer_name", "--output", "json",
        ]
        code = cli_main(argv)
        payload = json.loads(capsys.readouterr().out)
        assert code == 0
        assert payload["is_match"]


class TestNullRenderedAsNullNotBackslashN:
    """A changed row where one side is a real SQL NULL: NORMALISE wraps it
    in the NULL-1 canonical marker `\\N` for hashing/comparison (spec
    §6.1), but a human reading terminal or JSON output should see "NULL",
    never a raw `\\N` that reads as a mangled value.
    """

    def test_terminal_output_shows_null_not_backslash_n(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, NULL)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, 'not null anymore')")

        argv = ["diff", f"{pg_database}/a", f"{pg_database}/b", "--key", "id"]
        code = cli_main(argv)
        out = capsys.readouterr().out

        assert code == 1
        assert "NULL" in out
        assert "\\N" not in out

    def test_json_output_shows_null_not_backslash_n(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, NULL)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1, 'not null anymore')")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--output", "json",
        ]
        code = cli_main(argv)
        payload = json.loads(capsys.readouterr().out)

        assert code == 1
        change = payload["row_diffs"][0]["changes"]["name"]
        assert change["source"] == "NULL"
        assert change["target"] == "not null anymore"


class TestJsonOutputM1Fields:
    def test_json_output_carries_excluded_columns_timings_and_sql(self, pg_database, capsys):
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text, extra_a boolean)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g, true FROM generate_series(1, 10) g")
        exec_sql(pg_database, "CREATE TABLE b (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO b SELECT g, 'row-' || g FROM generate_series(1, 10) g")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--output", "json", "--verbose",
        ]
        code = cli_main(argv)
        out = capsys.readouterr().out
        payload = json.loads(out)

        assert code == 0
        assert payload["schema_version"] == 1
        assert payload["excluded_columns"] == ["extra_a"]
        assert payload["timings"]["total_seconds"] >= 0
        assert len(payload["sql_statements"]) > 0
        assert any("SELECT" in s for s in payload["sql_statements"])

    def test_json_sql_statements_include_every_segment_hash_query_at_default_threads(
        self, pg_database, capsys
    ):
        # Regression: with the default --threads 4, the per-segment hash
        # queries run on extra pooled connections that were never hooked
        # into the SQL log, so the JSON (and the HTML "Reproduce" section,
        # which shares the same log) listed only the setup queries -- not
        # the hash queries that actually prove the match. spec §8.2/§8.3:
        # "every generated SQL statement".
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 5000) g")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b SELECT * FROM a")

        argv = [
            "diff", f"{pg_database}/a", f"{pg_database}/b",
            "--key", "id", "--algorithm", "hashdiff", "--output", "json",
        ]
        code = cli_main(argv)
        payload = json.loads(capsys.readouterr().out)

        assert code == 0
        hash_queries = [s for s in payload["sql_statements"] if "md5" in s.lower()]
        # identical tables never bisect: exactly one hash query per segment, per side
        assert len(hash_queries) == 2 * payload["queries_per_side"], (
            f"expected {2 * payload['queries_per_side']} hash queries recorded, "
            f"got {len(hash_queries)}"
        )
