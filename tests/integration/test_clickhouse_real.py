"""M2's REAL acceptance tests (spec §13 M2) — against an actual ClickHouse
server, executing actual generated SQL. This is the test spec §5 calls
non-negotiable ("add a cross-engine test that hashes the same fixture
string on every engine and asserts equality") and the test spec §0 rule 3
requires before M2 can be called done ("verify on real databases before
calling a milestone done").

*** THIS FILE CANNOT RUN IN THIS DEV ENVIRONMENT. ***
No Docker daemon, no network access to install a ClickHouse server or
client (apt/pip both blocked — see docs/DEV_ENVIRONMENT.md's ClickHouse
section), no pre-installed binary, and the user's linked desktop hit an
unrelated Windows-bridge bug when we tried that route too. The tests below
are written exactly as they should run once a real server is reachable —
point `TABLEDIFF_TEST_CH_DSN` at one (default assumes a local instance on
the standard HTTP port) and this file collects and runs for real, no code
changes needed. Until then, every test here is SKIPPED (not faked as
passing, not silently ignored — see `_require_clickhouse` below, which
prints exactly why every time) and M2's cross-engine acceptance criteria
remain unverified. Do not report M2 done while this file's tests are
skipped.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tablediff.connectors import _pgwire
from tablediff.connectors._chwire import ConnectionFailedError, check_connection, parse_ch_dsn, run_query
from tablediff.connectors.clickhouse import ClickHouseConnector
from tablediff.connectors.postgres import PostgresConnector
from tablediff.core.hashdiff import diff as hashdiff
from tablediff.core.models import TableRef

# Defaults match docker-compose.yml exactly (Postgres 16 on 5432,
# ClickHouse 24.8 on 8123 with CLICKHOUSE_PASSWORD=clickhouse) — no env
# vars needed when running against the Docker services this project's
# docker-compose.yml starts. TABLEDIFF_TEST_CH_DSN / _PG_ADMIN_DSN still
# override for any other reachable instance.
CH_ADMIN_DSN_URL = os.environ.get(
    "TABLEDIFF_TEST_CH_DSN", "clickhouse://default:clickhouse@127.0.0.1:8123/default"
)
PG_ADMIN_DSN_URL = os.environ.get(
    "TABLEDIFF_TEST_PG_ADMIN_DSN", "postgres://postgres:postgres@127.0.0.1:5432/postgres"
)


def _ch_dsn_for_database(database: str) -> str:
    """Build a DSN pointed at a specific database, reusing
    CH_ADMIN_DSN_URL's own host/port/credentials — never hard-coding them
    — so a TABLEDIFF_TEST_CH_DSN override is honoured everywhere, not just
    for the admin connection."""
    admin = parse_ch_dsn(CH_ADMIN_DSN_URL)
    auth = admin.user if admin.password is None else f"{admin.user}:{admin.password}"
    return f"clickhouse://{auth}@{admin.host}:{admin.port}/{database}"


@pytest.fixture(scope="module", autouse=True)
def _require_clickhouse():
    """Unlike conftest.py's `_require_postgres` (which pytest.exit's the
    whole session — appropriate there, since Postgres is a firm
    prerequisite this whole dev environment is expected to have), this
    SKIPS just this module's tests when ClickHouse isn't reachable. M2 is
    an in-progress milestone with a known, explained environment gap
    (see module docstring) — the rest of the suite (158+ tests covering
    M0/M1 and this milestone's own engine-agnostic unit tests) must keep
    running and passing regardless.
    """
    try:
        check_connection(parse_ch_dsn(CH_ADMIN_DSN_URL))
    except ConnectionFailedError as e:
        pytest.skip(
            f"ClickHouse is not reachable at {CH_ADMIN_DSN_URL!r} in this environment "
            f"({e}) — M2's real cross-engine acceptance tests cannot run here. See this "
            f"file's module docstring. M2 is not being reported done while this is skipped."
        )


def _ch_conn() -> ClickHouseConnector:
    c = ClickHouseConnector()
    c.connect(CH_ADMIN_DSN_URL)
    return c


def _pg_conn() -> PostgresConnector:
    c = PostgresConnector()
    c.connect(PG_ADMIN_DSN_URL)
    return c


@pytest.fixture
def ch_database():
    dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
    name = "tablediff_test_" + uuid.uuid4().hex[:16]
    run_query(dsn, f"CREATE DATABASE `{name}`")
    try:
        yield name
    finally:
        run_query(dsn, f"DROP DATABASE IF EXISTS `{name}`")


@pytest.fixture
def pg_database():
    dsn = _pgwire.parse_pg_dsn(PG_ADMIN_DSN_URL)
    name = "tablediff_test_" + uuid.uuid4().hex[:16]
    _pgwire.run_query(dsn, f'CREATE DATABASE "{name}"')
    try:
        yield f"postgres://postgres:postgres@127.0.0.1:5432/{name}"
    finally:
        _pgwire.run_query(dsn, f'DROP DATABASE IF EXISTS "{name}"')


class TestCrossEngineHashEqualityLive:
    """spec §5's non-negotiable test, for real: the SAME fixture string,
    hashed by each engine's ACTUAL generated SQL, must produce the SAME
    64-bit integer."""

    @pytest.mark.parametrize("fixture_string", [
        "hello", "", "row-2", "12.50", "unicode: café ☃",
    ])
    def test_same_fixture_string_hashes_equal_on_both_engines(self, fixture_string, ch_database):
        pg = _pg_conn()
        ch = _ch_conn()
        pg_lit = pg.quote_literal(fixture_string)
        ch_lit = ch.quote_literal(fixture_string)
        pg_value = pg.query(f"SELECT {pg.row_hash_expr([pg_lit])}")[0][0]
        ch_value = ch.query(f"SELECT {ch.row_hash_expr([ch_lit])}")[0][0]
        assert pg_value == ch_value, (
            f"row_hash_expr disagreed between Postgres and ClickHouse for "
            f"{fixture_string!r}: postgres={pg_value} clickhouse={ch_value}"
        )


class TestTs2AcrossEngines:
    def test_timestamptz_vs_datetime_same_second_is_not_reported(self, pg_database, ch_database):
        from .conftest import exec_sql

        # .354321 (not .654321): Postgres's own typmod cast to precision 0
        # ROUNDS, not truncates (empirically confirmed and deliberately
        # chosen at M1 — see test_m1_fixtures.py's
        # test_rounds_half_up_at_lower_precision_not_truncates), so a
        # fractional value past the .5 boundary would round UP into the
        # *next* second and genuinely differ from ClickHouse's whole-second
        # value here — this fixture has to stay under that boundary to
        # actually exercise "differences within the same second are not
        # reported" (spec §13 M2) rather than a real, correctly-reported
        # difference.
        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, ts timestamptz(6))")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '2024-03-01 10:00:00.354321+00')")

        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b (id UInt64, ts DateTime) ENGINE = Memory")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b VALUES (1, '2024-03-01 10:00:00')")

        source = PostgresConnector()
        source.connect(pg_database)
        target = ClickHouseConnector()
        target.connect(_ch_dsn_for_database(ch_database))

        result = hashdiff(
            source, target,
            TableRef(engine="postgres", database=pg_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert any(w.rule and w.rule.value == "TS-2" for w in result.warnings)


class TestDecimalAcrossEngines:
    def test_numeric_12_2_vs_decimal_12_4_compares_at_scale_2(self, pg_database, ch_database):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, total numeric(12,2))")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 12.50)")

        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b (id UInt64, total Decimal(12,4)) ENGINE = Memory")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b VALUES (1, 12.5000)")

        source = PostgresConnector()
        source.connect(pg_database)
        target = ClickHouseConnector()
        target.connect(_ch_dsn_for_database(ch_database))

        result = hashdiff(
            source, target,
            TableRef(engine="postgres", database=pg_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert any(w.rule and w.rule.value == "DEC-1" for w in result.warnings)


class TestNullableAcrossEngines:
    def test_clickhouse_nullable_string_null_equals_postgres_null(self, pg_database, ch_database):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, NULL)")

        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b (id UInt64, name Nullable(String)) ENGINE = Memory")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b VALUES (1, NULL)")

        source = PostgresConnector()
        source.connect(pg_database)
        target = ClickHouseConnector()
        target.connect(_ch_dsn_for_database(ch_database))

        result = hashdiff(
            source, target,
            TableRef(engine="postgres", database=pg_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs


class TestClickHouseKeySegmentation:
    """spec §9 M2: "segment translation for UInt keys" — core/hashdiff.py's
    segment-and-bisect path (_key_expr_for_ordering / _bounds_key_expr) used
    to hard-code Postgres-only SQL with no ClickHouse case at all: forcing
    `COLLATE "C"` on any non-uuid text key (a syntax error in ClickHouse —
    confirmed against a real server, not just reasoned through) and a
    `CAST(... AS TEXT) COLLATE "C"` workaround for uuid MIN/MAX that
    ClickHouse doesn't even need (it has a native MIN/MAX(UUID) aggregate,
    unlike Postgres). These tests cover all three ClickHouse key shapes a
    real migration's primary key is likely to be: UInt64 (already worked,
    since numeric keys never touch that code path — kept here as a
    regression test), String, and UUID."""

    def _ch_conn_for(self, ch_database: str) -> ClickHouseConnector:
        c = ClickHouseConnector()
        c.connect(_ch_dsn_for_database(ch_database))
        return c

    def test_uint64_key_segments_and_matches(self, ch_database):
        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.a (id UInt64, name String) ENGINE = MergeTree ORDER BY id")
        run_query(
            ch_dsn,
            f"INSERT INTO `{ch_database}`.a SELECT number, 'row-' || toString(number) FROM numbers(20000)",
        )
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b AS `{ch_database}`.a")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b SELECT * FROM `{ch_database}`.a")

        source = self._ch_conn_for(ch_database)
        target = self._ch_conn_for(ch_database)
        result = hashdiff(
            source, target,
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert result.source_count == 20000

    def test_string_key_segments_and_matches(self, ch_database):
        """Before the fix: errors with a ClickHouse syntax error on
        `COLLATE "C"` — this table never even gets past the bounds query."""
        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.a (id String, name String) ENGINE = MergeTree ORDER BY id")
        run_query(
            ch_dsn,
            f"INSERT INTO `{ch_database}`.a SELECT 'k' || leftPad(toString(number), 8, '0'), 'row' "
            "FROM numbers(3000)",
        )
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b AS `{ch_database}`.a")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b SELECT * FROM `{ch_database}`.a")

        source = self._ch_conn_for(ch_database)
        target = self._ch_conn_for(ch_database)
        result = hashdiff(
            source, target,
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert result.source_count == 3000

    def test_uuid_key_segments_and_matches(self, ch_database):
        """Before the fix: errors on the bounds query's `CAST(... AS TEXT)
        COLLATE "C"` uuid workaround — also a ClickHouse syntax error, and
        also unnecessary there since ClickHouse has a native MIN/MAX(UUID)
        aggregate Postgres lacks."""
        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.a (id UUID, name String) ENGINE = MergeTree ORDER BY id")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.a SELECT generateUUIDv4(), 'row' FROM numbers(3000)")
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b AS `{ch_database}`.a")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b SELECT * FROM `{ch_database}`.a")

        source = self._ch_conn_for(ch_database)
        target = self._ch_conn_for(ch_database)
        result = hashdiff(
            source, target,
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert result.source_count == 3000


class TestClickHouseSpecificTypes:
    """spec §9 M2: "Nullable() handling; LowCardinality, Enum8/16, Array,
    Map (as text, UNK-1) types" — a real end-to-end diff exercising every
    one of these column shapes together against a live server, not just
    the unit-level _resolve()/_unwrap() string mapping (which can't prove
    the generated SQL actually executes -- exactly the class of gap that
    let the real DEC-1 bugs through unit tests alone)."""

    def test_enum_array_map_lowcardinality_nullable_self_diff_matches(self, ch_database):
        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        ddl = (
            "id UInt64, status Enum8('active' = 1, 'inactive' = 2), "
            "tags Array(String), meta Map(String, UInt32), "
            "label LowCardinality(String), note Nullable(String)"
        )
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.a ({ddl}) ENGINE = MergeTree ORDER BY id")
        run_query(
            ch_dsn,
            f"INSERT INTO `{ch_database}`.a VALUES "
            "(1, 'active', ['a','b'], {'x':1,'y':2}, 'cat1', 'hi'), "
            "(2, 'inactive', [], {}, 'cat2', NULL)",
        )
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b AS `{ch_database}`.a")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b SELECT * FROM `{ch_database}`.a")

        source = ClickHouseConnector()
        source.connect(_ch_dsn_for_database(ch_database))
        target = ClickHouseConnector()
        target.connect(_ch_dsn_for_database(ch_database))

        result = hashdiff(
            source, target,
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="b"),
            key_columns=["id"],
        )
        assert result.is_match, result.row_diffs
        assert result.source_count == 2
        # Map has no dedicated rule (spec: "as text, UNK-1") -- Array and
        # Enum8 do (ARR-1, ENUM-1), so only "meta" should warn UNK-1.
        unk1_columns = {w.column for w in result.warnings if w.rule and w.rule.value == "UNK-1"}
        assert unk1_columns == {"meta"}, result.warnings

    def test_enum_array_map_reports_a_real_change(self, ch_database):
        """Same shapes, but this time the two sides genuinely differ in
        each of Enum8, Array, Map and LowCardinality columns -- proving
        these render distinctly, not just identically."""
        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        ddl = (
            "id UInt64, status Enum8('active' = 1, 'inactive' = 2), "
            "tags Array(String), meta Map(String, UInt32), label LowCardinality(String)"
        )
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.a ({ddl}) ENGINE = MergeTree ORDER BY id")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.a VALUES (1, 'active', ['a','b'], {{'x':1}}, 'cat1')")
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b AS `{ch_database}`.a")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b VALUES (1, 'inactive', ['a','c'], {{'x':2}}, 'cat2')")

        source = ClickHouseConnector()
        source.connect(_ch_dsn_for_database(ch_database))
        target = ClickHouseConnector()
        target.connect(_ch_dsn_for_database(ch_database))

        result = hashdiff(
            source, target,
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="a"),
            TableRef(engine="clickhouse", database=ch_database, schema=ch_database, table="b"),
            key_columns=["id"],
        )
        assert not result.is_match
        assert result.changed == 1
        changed_cols = set(result.row_diffs[0].changes)
        assert changed_cols == {"status", "tags", "meta", "label"}, result.row_diffs[0].changes


class TestCliSupportsClickHouse:
    """The CLI's connector factory used to have ClickHouse commented out
    ("-- M2") -- `tablediff diff` itself, not just calling hashdiff()
    directly, needs to actually support a ClickHouse source/target now
    that M2 is real."""

    def test_cli_diff_postgres_to_clickhouse_returns_exit_0_on_match(self, pg_database, ch_database, capsys):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, name text)")
        exec_sql(pg_database, "INSERT INTO a SELECT g, 'row-' || g FROM generate_series(1, 500) g")

        ch_dsn = parse_ch_dsn(CH_ADMIN_DSN_URL)
        run_query(ch_dsn, f"CREATE TABLE `{ch_database}`.b (id UInt64, name String) ENGINE = MergeTree ORDER BY id")
        run_query(ch_dsn, f"INSERT INTO `{ch_database}`.b SELECT number, 'row-' || toString(number) FROM numbers(1, 500)")

        admin = parse_ch_dsn(CH_ADMIN_DSN_URL)
        auth = admin.user if admin.password is None else f"{admin.user}:{admin.password}"
        ch_url = f"clickhouse://{auth}@{admin.host}:{admin.port}/{ch_database}/b"

        from tablediff.cli.main import main as cli_main

        argv = ["diff", f"{pg_database}/a", ch_url, "--key", "id"]
        code = cli_main(argv)
        captured = capsys.readouterr()

        assert code == 0, captured.err
        assert "MATCH" in captured.out
