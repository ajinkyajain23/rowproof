"""Postgres wire access, via `psycopg` (v3) — the real spec §3 driver.

`PostgresConnector` in postgres.py calls only the functions below, never
psycopg directly, so the rest of the codebase is insulated from driver
specifics. This module used to shell out to the `psql` CLI binary (see
git history / docs/DEV_ENVIRONMENT.md for why); psycopg replaces that
with a real wire-protocol connection and hands back already-typed Python
values (int, Decimal, datetime, bool, ...) instead of text that has to be
guessed back into a type.

Two query paths, matching two different needs:

* `open_connection()` / `run_query_on()` / `close_connection()` — a
  genuinely persistent connection, opened once by `connect()` and reused
  for every query for the lifetime of a diff. This is what spec §5 means
  by "One connection per side. No connection pooling in v1." and is
  performance-critical: a 100M-row diff issues thousands of small
  per-segment queries (spec §13's 100M-row/5-minute benchmark), and
  reconnecting for each one measured at ~17ms of pure overhead against a
  local Postgres — at a few thousand queries that alone would blow the
  time budget several times over.
* `run_query()` / `check_connection()` — a one-shot, self-contained
  query against a fresh connection, for the handful of call sites that
  genuinely want that: a stateless reachability probe (`check_connection`,
  used both by `connect()` below and by test fixtures polling "is the
  server back up yet") and test-only admin operations (creating/dropping
  whole scratch databases — see `tests/integration/conftest.py`'s
  `exec_sql`), which have no diff-lifetime connection to reuse anyway.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import psycopg


class ConnectionFailedError(Exception):
    pass


class QueryFailedError(Exception):
    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


@dataclass(frozen=True)
class PgDsn:
    host: str
    port: int
    user: str
    password: str | None
    database: str


def parse_pg_dsn(dsn: str) -> PgDsn:
    """Parse `postgres://user:pw@host:port/database`.

    Only the connection portion is handled here — a trailing
    `/schema.table` (spec §7's SOURCE/TARGET grammar) is the CLI layer's
    job to split off before calling connect().
    """
    parts = urlsplit(dsn)
    if parts.scheme not in ("postgres", "postgresql"):
        raise ValueError(f"not a postgres DSN: {dsn!r}")
    database = parts.path.lstrip("/")
    return PgDsn(
        host=parts.hostname or "localhost",
        port=parts.port or 5432,
        user=unquote(parts.username) if parts.username else "",
        password=unquote(parts.password) if parts.password else None,
        database=database,
    )


def _connect_kwargs(dsn: PgDsn, connect_timeout: float | None) -> dict:
    kwargs = {
        "host": dsn.host,
        "port": dsn.port,
        "user": dsn.user,
        "dbname": dsn.database,
        "autocommit": True,
    }
    if dsn.password is not None:
        kwargs["password"] = dsn.password
    if connect_timeout:
        # libpq's connect_timeout is whole seconds; round up so a small
        # fractional timeout doesn't collapse to 0 (which libpq treats as
        # "no timeout" — the opposite of what a caller asking for a short
        # timeout means).
        kwargs["connect_timeout"] = max(1, int(connect_timeout + 0.999))
    return kwargs


def _classify(e: Exception, sql: str, timeout: float | None) -> None:
    """Re-raise a psycopg exception as the rowproof-typed error the rest
    of the codebase expects. Shared by both the persistent-connection and
    one-shot query paths so the two stay classified identically."""
    if isinstance(e, psycopg.errors.QueryCanceled):
        raise QueryFailedError(f"query timed out after {timeout}s", sql) from e
    if isinstance(e, psycopg.OperationalError):
        # Every libpq-level failure (refused/reset connection, auth
        # failure, unreachable host, dropped mid-query) surfaces as
        # OperationalError in psycopg — see spec §10's error-path list.
        raise ConnectionFailedError(str(e)) from e
    if isinstance(e, psycopg.Error):
        raise QueryFailedError(str(e), sql) from e
    raise e


def open_connection(dsn: PgDsn, timeout: float | None = 10.0) -> psycopg.Connection:
    """Open and return a single persistent connection — the one this side
    of a diff uses for every query until `close_connection()`."""
    try:
        return psycopg.connect(**_connect_kwargs(dsn, timeout))
    except psycopg.Error as e:
        _classify(e, "<connect>", timeout)
        raise  # pragma: no cover - _classify always raises for psycopg.Error


def close_connection(conn: psycopg.Connection) -> None:
    conn.close()


def run_query_on(conn: psycopg.Connection, sql: str, timeout: float | None = None) -> list[tuple]:
    """Run `sql` on an already-open persistent connection and return rows
    as tuples. This is the hot path every per-segment query in a diff
    goes through — no new connection, no re-authentication."""
    try:
        if timeout:
            # Server-side safety net so a hung query can't block forever.
            conn.execute(f"SET statement_timeout = {int(timeout * 1000)}")
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description is None:
                return []
            return [tuple(row) for row in cur.fetchall()]
    except psycopg.Error as e:
        _classify(e, sql, timeout)
        raise  # pragma: no cover


def check_connection(dsn: PgDsn, timeout: float = 10.0) -> None:
    try:
        run_query(dsn, "SELECT 1", timeout=timeout)
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e


def run_query(dsn: PgDsn, sql: str, timeout: float | None = 60.0) -> list[tuple]:
    """Open a fresh connection, run `sql`, return rows as tuples, close.

    A deliberate one-shot path — for `check_connection`'s stateless
    reachability probes and test-only admin operations, never for the
    diff hot path (see module docstring; `PostgresConnector.query()` uses
    `run_query_on()` on its one persistent connection instead).
    """
    try:
        with psycopg.connect(**_connect_kwargs(dsn, timeout)) as conn:
            return run_query_on(conn, sql, timeout)
    except psycopg.Error as e:
        _classify(e, sql, timeout)
        raise  # pragma: no cover
