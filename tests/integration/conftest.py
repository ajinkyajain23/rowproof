"""Fixtures for M0's real-database acceptance tests (spec §13: "Verify on
real databases before calling a milestone done.").

This dev environment has no Docker daemon, so these tests run against a
natively-installed local Postgres 16 instead of the spec's chosen
`testcontainers` (see docs/DEV_ENVIRONMENT.md). Every fixture here is
scoped narrowly to "how do I get a Postgres to point at" so that swapping
in testcontainers later is a fixture-only change — the tests themselves
only ever see a DSN.
"""

from __future__ import annotations

import os
import uuid

import pytest

from tablediff.connectors import _pgwire

ADMIN_DSN_URL = os.environ.get(
    "TABLEDIFF_TEST_PG_ADMIN_DSN", "postgres://postgres:postgres@127.0.0.1:5432/postgres"
)
HOST_PORT_USER_PW = os.environ.get(
    "TABLEDIFF_TEST_PG_BASE", "postgres:postgres@127.0.0.1:5432"
)


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
    name = "tablediff_test_" + uuid.uuid4().hex[:16]
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
