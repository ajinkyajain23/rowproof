"""M3's REAL acceptance tests (spec §13 M3, Snowflake half) — against an
actual Snowflake trial account, executing actual generated SQL. Covers
spec §9/§13 M3's three named Snowflake requirements (`NUMBER(38,0)` as
integer, `VARIANT` as text, `TIMESTAMP_NTZ/LTZ/TZ` rules), the row-hash
cross-engine equality spec §5 calls non-negotiable, the uuid-vs-VARCHAR
compatibility spec §6.2 describes, and a clean-error check for bad
credentials (spec §10).

Point `ROWPROOF_TEST_SF_DSN` at a reachable account
(`snowflake://user:pw@account/database`) and this file runs for real; a
per-test SCHEMA is created inside that database and dropped after (schema
create/drop is a metadata-only operation, no warehouse credits spent —
only the actual row-level queries below run on the X-Small,
auto-suspending warehouse the account was set up with). Every table here
is a handful of rows, per the explicit "keep Snowflake tests small"
instruction — this suite is about type/value correctness, not scale
(that's what M2's ClickHouse benchmark already covers for the algorithm
itself, which is engine-agnostic).

Snowflake identifiers created without quotes are stored upper-case
(confirmed against a real account); every table/column name below is
explicitly double-quoted lower-case at CREATE time so `--key`/column
names can be passed in the same lower-case spelling used throughout the
rest of this project's tests — SnowflakeConnector.get_schema() always
reports the real, verbatim stored name (same contract as every other
connector), so an unquoted (upper-case) table would need `--key ID` etc.
instead; this is a documented scope limit (see snowflake.py's own module
docstring), not something core/ special-cases.
"""

from __future__ import annotations

import pytest

from rowproof.connectors._sfwire import ConnectionFailedError
from rowproof.connectors.postgres import PostgresConnector
from rowproof.connectors.snowflake import SnowflakeConnector
from rowproof.core.hashdiff import diff as hashdiff
from rowproof.core.models import TableRef

from .conftest import SF_DSN_URL, _sf_dsn

PG_ADMIN_DSN_URL = "postgres://postgres:postgres@127.0.0.1:5432/postgres"

pytestmark = pytest.mark.usefixtures("_require_snowflake")


def _sf_conn() -> SnowflakeConnector:
    c = SnowflakeConnector()
    c.connect(SF_DSN_URL)
    return c


def _pg_conn() -> PostgresConnector:
    c = PostgresConnector()
    c.connect(PG_ADMIN_DSN_URL)
    return c


def _diff(pg_database, sf_schema, pg_table="a", sf_table="b", key_columns=("id",)):
    source = PostgresConnector()
    source.connect(pg_database)
    target = _sf_conn()
    return hashdiff(
        source, target,
        TableRef(engine="postgres", database=pg_database, table=pg_table),
        TableRef(engine="snowflake", database=_sf_dsn().database, table=sf_table, schema=sf_schema),
        key_columns=list(key_columns), max_diff_rows=100,
    )


class TestCrossEngineHashEqualityLive:
    """spec §5's non-negotiable test, for real: the SAME fixture string,
    hashed by each engine's ACTUAL generated SQL, must produce the SAME
    64-bit integer."""

    @pytest.mark.parametrize("fixture_string", [
        "hello", "", "row-2", "12.50", "unicode: café ☃",
    ])
    def test_same_fixture_string_hashes_equal_on_both_engines(self, fixture_string):
        pg = _pg_conn()
        sf = _sf_conn()
        try:
            pg_lit = pg.quote_literal(fixture_string)
            sf_lit = sf.quote_literal(fixture_string)
            pg_value = pg.query(f"SELECT {pg.row_hash_expr([pg_lit])}")[0][0]
            sf_value = sf.query(f"SELECT {sf.row_hash_expr([sf_lit])}")[0][0]
            assert pg_value == sf_value, (
                f"row_hash_expr disagreed between Postgres and Snowflake for "
                f"{fixture_string!r}: postgres={pg_value} snowflake={sf_value}"
            )
        finally:
            pg.close()
            sf.close()


class TestNumberAsIntegerAcrossEngines:
    def test_number_38_0_vs_postgres_bigint_equal_for_same_values(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, 'CREATE TABLE a (id bigint PRIMARY KEY, "n" bigint)')
        exec_sql(pg_database, 'INSERT INTO a VALUES (1, 9223372036854775807), (2, -9223372036854775808)')
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "n" NUMBER(38,0))')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES '
                "(1, 9223372036854775807), (2, -9223372036854775808)"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs
        # The 'n' column must have been compared via INT-1, not excluded
        # or silently DEC-1'd -- proven by the MATCH above plus no
        # "incompatible types"/DEC-1 warning for it.
        assert not any("n" in w.message and "incompatible" in w.message for w in result.warnings)


class TestTimestampRulesAcrossEngines:
    def test_timestamp_ntz_gets_ts_3_naive_warning(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, ts timestamp)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '2024-03-01 10:00:00')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "ts" TIMESTAMP_NTZ(6))')
            sf.query(f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES (1, \'2024-03-01 10:00:00\')')
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs
        # spec §9/§13 M3: "TIMESTAMP_NTZ handled with TS-3 warning"
        assert any("TS-3" in w.message and "naive" in w.message for w in result.warnings), result.warnings

    def test_timestamp_tz_converts_to_utc(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, ts timestamptz)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '2024-03-01 10:00:00+05:30')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "ts" TIMESTAMP_TZ(6))')
            sf.query(f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES (1, \'2024-03-01 10:00:00+05:30\')')
        finally:
            sf.close()
        # spec §9/§13 M3: "TIMESTAMP_TZ converts to UTC" -- both sides
        # name the SAME instant via different offsets; a MATCH here IS
        # the proof the UTC conversion happened correctly on both sides.
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_timestamp_ltz_also_converts_to_utc(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, ts timestamptz)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '2024-03-01 04:30:00+00')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "ts" TIMESTAMP_LTZ(6))')
            sf.query("ALTER SESSION SET TIMEZONE = 'UTC'")
            sf.query(f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES (1, \'2024-03-01 04:30:00\')')
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_a_genuinely_different_timestamp_is_still_reported(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, ts timestamptz)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, '2024-03-01 10:00:00+05:30')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "ts" TIMESTAMP_TZ(6))')
            sf.query(f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES (1, \'2024-03-01 10:00:01+05:30\')')
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert not result.is_match
        assert result.changed == 1
        rule = result.row_diffs[0].changes["ts"][2]
        assert rule.value == "TS-1"


class TestVariantAsTextAcrossEngines:
    def test_variant_string_scalar_compares_as_text(self, pg_database, sf_schema):
        # spec §9/§13 M3: "VARIANT as text" -- TO_VARCHAR(PARSE_JSON('"x"'))
        # unwraps to the bare string 'x' (confirmed against a real
        # account), so a Postgres text column holding the literal 'hello'
        # is the honest cross-engine-equal counterpart, not a JSON-vs-text
        # canonicalisation-matching exercise (UNK-1 promises "as text",
        # not "as engine-canonicalised JSON" the way JSON-1 does).
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'hello')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "v" VARIANT)')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" '
                "SELECT 1, PARSE_JSON('\"hello\"')"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_variant_change_is_detected_and_never_crashes(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, v text)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'hello')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "v" VARIANT)')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" '
                "SELECT 1, PARSE_JSON('\"goodbye\"')"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert not result.is_match
        assert result.changed == 1
        # The reported rule reflects the SOURCE side's own classification
        # (Postgres `text` -> STR-1); the target's VARIANT column renders
        # via its own UNK-1 fallback regardless -- both are "as text",
        # just labelled by whichever side's rule the row-diff reports.
        assert result.row_diffs[0].changes["v"][2].value == "STR-1"


class TestUuidVsVarcharAcrossEngines:
    def test_hyphenated_lowercase_uuid_matches(self, pg_database, sf_schema):
        # spec §6.2: "A Postgres uuid against a Snowflake VARCHAR compares
        # after UUID-1."
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, uid uuid)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "uid" VARCHAR(36))')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES '
                "(1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11')"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_uppercase_no_hyphen_uuid_still_matches(self, pg_database, sf_schema):
        # spec §6.3: "uuids: upper vs lower case; with vs without hyphens
        # (Snowflake)" -- the exact edge case named there.
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, uid uuid)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "uid" VARCHAR(36))')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES '
                "(1, 'A0EEBC999C0B4EF8BB6D6BB9BD380A11')"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert result.is_match, result.row_diffs

    def test_genuinely_different_uuid_is_reported_with_uuid_1_rule(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, uid uuid)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 'a0eebc99-9c0b-4ef8-bb6d-6bb9bd380a11')")
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "uid" VARCHAR(36))')
            sf.query(
                f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES '
                "(1, 'B0EEBC999C0B4EF8BB6D6BB9BD380A22')"
            )
        finally:
            sf.close()
        result = _diff(pg_database, sf_schema)
        assert not result.is_match
        assert result.row_diffs[0].changes["uid"][2].value == "UUID-1"


class TestPrimaryKeyDetection:
    def test_show_primary_keys_reports_declared_key(self, sf_schema):
        sf = _sf_conn()
        try:
            sf.query(
                f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."pk_t" '
                '("id" NUMBER(38,0), "ts" NUMBER(38,0), PRIMARY KEY ("id", "ts"))'
            )
            pk = sf.get_primary_key(
                TableRef(engine="snowflake", database=_sf_dsn().database, table="pk_t", schema=sf_schema)
            )
            assert pk == ["id", "ts"]
        finally:
            sf.close()

    def test_returns_none_when_no_primary_key_declared(self, sf_schema):
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."no_pk_t" ("id" NUMBER(38,0))')
            pk = sf.get_primary_key(
                TableRef(engine="snowflake", database=_sf_dsn().database, table="no_pk_t", schema=sf_schema)
            )
            assert pk is None
        finally:
            sf.close()


class TestCleanErrorForBadCredentials:
    def test_bad_password_raises_connection_failed_not_a_crash(self):
        # spec §10: "Clean error for: bad credentials."
        from rowproof.connectors._sfwire import ConnectionFailedError

        real = _sf_dsn()
        bad_dsn = f"snowflake://{real.user}:definitely-the-wrong-password@{real.account}/{real.database}"
        sf = SnowflakeConnector()
        with pytest.raises(ConnectionFailedError):
            sf.connect(bad_dsn)


class TestBooleanVsIntegerAcrossEngines:
    """BOOL-1 for an integer paired with a boolean (found migrating Pagila
    to ClickHouse; the Snowflake rendering is separate SQL, so it needs its
    own real-server proof)."""

    def _sf_table(self, sf_schema, ddl, rows):
        sf = _sf_conn()
        try:
            sf.query(f'CREATE TABLE "{_sf_dsn().database}"."{sf_schema}"."b" ("id" NUMBER(38,0), "v" {ddl})')
            sf.query(f'INSERT INTO "{_sf_dsn().database}"."{sf_schema}"."b" VALUES {rows}')
        finally:
            sf.close()

    def test_postgres_boolean_vs_snowflake_number_matches(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, v boolean)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, true), (2, false), (3, NULL)")
        self._sf_table(sf_schema, "NUMBER(38,0)", "(1, 1), (2, 0), (3, NULL)")
        result = _diff(pg_database, sf_schema)
        assert result.excluded_columns == []
        assert result.is_match, result.row_diffs

    def test_snowflake_number_other_than_0_or_1_is_a_difference(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, v boolean)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, true), (2, true)")
        self._sf_table(sf_schema, "NUMBER(38,0)", "(1, 1), (2, 2)")
        result = _diff(pg_database, sf_schema)
        assert not result.is_match
        assert [rd.key[0] for rd in result.row_diffs] == [2]

    def test_postgres_integer_vs_snowflake_boolean_matches(self, pg_database, sf_schema):
        from .conftest import exec_sql

        exec_sql(pg_database, "CREATE TABLE a (id bigint PRIMARY KEY, v integer)")
        exec_sql(pg_database, "INSERT INTO a VALUES (1, 1), (2, 0)")
        self._sf_table(sf_schema, "BOOLEAN", "(1, true), (2, false)")
        result = _diff(pg_database, sf_schema)
        assert result.excluded_columns == []
        assert result.is_match, result.row_diffs
