"""Postgres wire access, via `psycopg` (v3) — the real spec §3 driver.

`PostgresConnector` in postgres.py calls only `run_query()` and
`check_connection()` below, never psycopg directly, so the rest of the
codebase is insulated from driver specifics. This module used to shell out
to the `psql` CLI binary (see git history / docs/DEV_ENVIRONMENT.md for
why); psycopg replaces that with a real wire-protocol connection and hands
back already-typed Python values (int, Decimal, datetime, bool, ...)
instead of text that has to be guessed back into a type.
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


def check_connection(dsn: PgDsn, timeout: float = 10.0) -> None:
    try:
        run_query(dsn, "SELECT 1", timeout=timeout)
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e


def run_query(dsn: PgDsn, sql: str, timeout: float | None = 60.0) -> list[tuple]:
    """Open a fresh connection, run `sql`, return rows as tuples, close.

    One connection per query, matching spec §5 ("One connection per side.
    No connection pooling in v1.") — v1 connects once per diff and reuses
    that connection for every query on a side, but this module's job is
    just "run this SQL against this DSN"; call-site pooling, if ever
    added, belongs in postgres.py, not here.
    """
    try:
        with psycopg.connect(**_connect_kwargs(dsn, timeout)) as conn:
            if timeout:
                # Server-side safety net so a hung query can't block
                # forever even though libpq's own connect_timeout only
                # covers establishing the connection, not query execution.
                conn.execute(f"SET statement_timeout = {int(timeout * 1000)}")
            with conn.cursor() as cur:
                cur.execute(sql)
                if cur.description is None:
                    return []
                return [tuple(row) for row in cur.fetchall()]
    except psycopg.errors.QueryCanceled as e:
        raise QueryFailedError(f"query timed out after {timeout}s", sql) from e
    except psycopg.OperationalError as e:
        # Every libpq-level failure (refused/reset connection, auth
        # failure, unreachable host, dropped mid-query) surfaces as
        # OperationalError in psycopg — see spec §10's error-path list.
        raise ConnectionFailedError(str(e)) from e
    except psycopg.Error as e:
        raise QueryFailedError(str(e), sql) from e
