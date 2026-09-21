"""Fixtures for M0's real-database acceptance tests (spec §13: "Verify on
real databases before calling a milestone done.").

This dev environment has no Docker daemon, so these tests run against a
natively-installed local Postgres 16 instead of the spec's chosen
`testcontainers` (see docs/internal/DEV_ENVIRONMENT.md). Every fixture here is
scoped narrowly to "how do I get a Postgres to point at" so that swapping
in testcontainers later is a fixture-only change — the tests themselves
only ever see a DSN.
"""

from __future__ import annotations

import os
import uuid

import pytest

from rowproof.connectors import _pgwire
from rowproof.connectors._sfwire import ConnectionFailedError as SfConnectionFailedError
from rowproof.connectors._sfwire import SfDsn, check_connection as sf_check_connection
from rowproof.connectors._sfwire import parse_sf_dsn, run_query as sf_run_query

ADMIN_DSN_URL = os.environ.get(
    "ROWPROOF_TEST_PG_ADMIN_DSN", "postgres://postgres:postgres@127.0.0.1:5432/postgres"
)
HOST_PORT_USER_PW = os.environ.get(
    "ROWPROOF_TEST_PG_BASE", "postgres:postgres@127.0.0.1:5432"
)

# spec §9/§13 M3: Snowflake, as an optional extra -- tests needing it (see
# test_snowflake_real.py's own module docstring) SKIP rather than fail
# when this isn't set, same pattern as ClickHouse's `_require_clickhouse`.
#
# Falls back to the pre-rename TABLEDIFF_TEST_SF_DSN name (M4: renamed
# tablediff -> rowproof) so a dev machine that already has the old name
# set in its OS environment doesn't silently lose Snowflake test coverage
# until someone remembers to re-set it under the new name -- this is a
# one-time migration bridge, not a permanent dual-name API.
SF_DSN_URL = os.environ.get("ROWPROOF_TEST_SF_DSN") or os.environ.get("TABLEDIFF_TEST_SF_DSN", "")


def _sf_dsn() -> SfDsn:
    return parse_sf_dsn(SF_DSN_URL)


def _admin_dsn():
    return _pgwire.parse_pg_dsn(ADMIN_DSN_URL)


def _run_admin(sql: str):
    return _pgwire.run_query(_admin_dsn(), sql)


@pytest.fixture(scope="session", autouse=True)
def _require_postgres():
    try:
        _pgwire.check_connection(_admin_dsn())
    except Exception as e:  # noqa: BLE001
        pytest.exit(
            f"Postgres is not reachable at {ADMIN_DSN_URL!r} — integration tests "
            f"cannot verify M0 acceptance criteria without it ({e}).",
            returncode=1,
        )


@pytest.fixture
def pg_database():
    """A freshly created, uniquely named database, dropped after the test.
    Yields its base DSN (no table suffix) — tests build TableRefs directly."""
    name = "rowproof_test_" + uuid.uuid4().hex[:16]
    _run_admin(f'CREATE DATABASE "{name}"')
    dsn_url = f"postgres://{HOST_PORT_USER_PW}/{name}"
    try:
        yield dsn_url
    finally:
        try:
            _run_admin(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname = '{name}' AND pid <> pg_backend_pid()"
            )
        except Exception:  # noqa: BLE001
            pass
        _run_admin(f'DROP DATABASE IF EXISTS "{name}"')


def exec_sql(dsn_url: str, sql: str):
    """Test-only helper for DDL/seeding — NOT part of the Connector
    protocol (spec rule 0.5: connectors are read-only by construction)."""
    return _pgwire.run_query(_pgwire.parse_pg_dsn(dsn_url), sql)


@pytest.fixture(scope="session")
def _require_snowflake():
    """SKIPS the requesting test/module when Snowflake isn't reachable
    (same pattern as test_clickhouse_real.py's own `_require_clickhouse`)
    — the rest of the suite must keep running and passing regardless. M3
    is not being reported done while this is skipped. Session-scoped
    (checked once, not per test) since it's a pure reachability probe with
    no side effects to isolate between tests.
    """
    if not SF_DSN_URL:
        pytest.skip(
            "ROWPROOF_TEST_SF_DSN is not set -- M3's Snowflake acceptance "
            "tests cannot run here."
        )
    try:
        sf_check_connection(_sf_dsn())
    except SfConnectionFailedError as e:
        pytest.skip(f"Snowflake is not reachable at the configured account ({e}).")


@pytest.fixture
def sf_schema(_require_snowflake):
    """A freshly created, uniquely named schema inside the DSN's own
    database, dropped after the test. Metadata-only DDL — costs no
    warehouse compute/credits, unlike the row-level queries each test
    itself runs (kept deliberately small — thousands of rows at most, not
    millions, to protect a trial account's credits)."""
    dsn = _sf_dsn()
    name = "rowproof_test_" + uuid.uuid4().hex[:16]
    sf_run_query(dsn, f'CREATE SCHEMA "{dsn.database}"."{name}"')
    try:
        yield name
    finally:
        sf_run_query(dsn, f'DROP SCHEMA IF EXISTS "{dsn.database}"."{name}"')
