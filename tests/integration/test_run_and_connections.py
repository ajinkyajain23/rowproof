"""`rowproof run CONFIG.yaml` and `rowproof connections test NAME`
(spec §7) against real Postgres.
"""

from __future__ import annotations

from rowproof.cli.main import main as cli_main

from .conftest import exec_sql


def write_config(tmp_path, content: str) -> str:
    p = tmp_path / "rowproof.yaml"
    p.write_text(content)
    return str(p)


class TestRunCommand:
    def test_run_reports_match_for_every_table_pair(self, pg_database, tmp_path, capsys):
        exec_sql(pg_database, "CREATE TABLE orders (id bigint PRIMARY KEY, total numeric(10,2))")
        exec_sql(pg_database, "INSERT INTO orders SELECT g, g * 1.5 FROM generate_series(1, 100) g")
        exec_sql(pg_database, "CREATE TABLE orders_copy (LIKE orders INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO orders_copy SELECT * FROM orders")

        exec_sql(pg_database, "CREATE TABLE customers (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO customers SELECT g, 'c-' || g FROM generate_series(1, 20) g")
        exec_sql(pg_database, "CREATE TABLE customers_copy (LIKE customers INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO customers_copy SELECT * FROM customers")

        config_path = write_config(
            tmp_path,
            f"""
            connections:
              db: {pg_database}
            tables:
              - source: db/public.orders
                target: db/public.orders_copy
                key: id
              - source: db/public.customers
                target: db/public.customers_copy
                key: id
            """,
        )

        code = cli_main(["run", config_path])
        out = capsys.readouterr().out
        assert code == 0
        assert out.count("MATCH") == 2

    def test_run_exit_code_is_worst_across_all_table_pairs(self, pg_database, tmp_path, capsys):
        exec_sql(pg_database, "CREATE TABLE a1 (id bigint PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO a1 VALUES (1, 'x')")
        exec_sql(pg_database, "CREATE TABLE a2 (LIKE a1 INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO a2 VALUES (1, 'x')")  # matches

        exec_sql(pg_database, "CREATE TABLE b1 (id bigint PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO b1 VALUES (1, 'x')")
        exec_sql(pg_database, "CREATE TABLE b2 (LIKE b1 INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b2 VALUES (1, 'y')")  # differs

        config_path = write_config(
            tmp_path,
            f"""
            connections:
              db: {pg_database}
            tables:
              - source: db/public.a1
                target: db/public.a2
                key: id
              - source: db/public.b1
                target: db/public.b2
                key: id
            """,
        )

        code = cli_main(["run", config_path])
        out = capsys.readouterr().out
        assert code == 1
        assert out.count("MATCH") == 1
        assert out.count("DIFFERENT") == 1

    def test_run_with_env_var_secret_in_dsn(self, pg_database, tmp_path, capsys, monkeypatch):
        # the pg_database DSN already contains "postgres:postgres" as
        # user:pw — sub it via an env var to prove ${VAR} substitution
        # actually resolves before the DSN is used to connect.
        monkeypatch.setenv("TD_TEST_PW", "postgres")
        dsn_with_var = pg_database.replace("postgres:postgres@", "postgres:${TD_TEST_PW}@")
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1)")
        exec_sql(pg_database, "CREATE TABLE b (LIKE a INCLUDING ALL)")
        exec_sql(pg_database, "INSERT INTO b VALUES (1)")

        config_path = write_config(
            tmp_path,
            f"""
            connections:
              db: {dsn_with_var}
            tables:
              - source: db/public.a
                target: db/public.b
                key: id
            """,
        )
        code = cli_main(["run", config_path])
        assert code == 0


class TestConnectionsTestCommand:
    def test_connections_test_reports_ok_for_a_reachable_connection(self, pg_database, tmp_path, capsys):
        config_path = write_config(
            tmp_path,
            f"""
            connections:
              db: {pg_database}
            tables: []
            """,
        )
        code = cli_main(["connections", "test", "db", "--config", config_path])
        out = capsys.readouterr().out
        assert code == 0
        assert "db: ok" in out

    def test_connections_test_fails_clearly_for_a_bad_connection(self, tmp_path, capsys):
        config_path = write_config(
            tmp_path,
            """
            connections:
              db: postgres://postgres:postgres@127.0.0.1:59999/nope
            tables: []
            """,
        )
        code = cli_main(["connections", "test", "db", "--config", config_path])
        err = capsys.readouterr().err
        assert code == 2
        assert "db" in err

    def test_connections_test_unknown_name_exits_2(self, pg_database, tmp_path, capsys):
        config_path = write_config(
            tmp_path,
            f"""
            connections:
              db: {pg_database}
            tables: []
            """,
        )
        code = cli_main(["connections", "test", "nonexistent", "--config", config_path])
        err = capsys.readouterr().err
        assert code == 2
        assert "unknown connection" in err
