"""Snowflake wire access, via `snowflake-connector-python` — the real spec
§3 driver (installed as the `tablediff[snowflake]` optional extra, per
spec's own "heavy dependency" note).

`SnowflakeConnector` in snowflake.py calls only the functions below, never
`snowflake.connector` directly. Same persistent-vs-one-shot split as
_pgwire.py/_chwire.py (see either module's own docstring for the full
performance rationale): `open_connection()`/`run_query_on()`/
`close_connection()` for the diff's hot path (one connection per side,
reused for every query, never reconnecting mid-diff), `run_query()`/
`check_connection()` for stateless reachability probes and test-only admin
operations that have no diff-lifetime connection to reuse.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import snowflake.connector
from snowflake.connector.errors import DatabaseError, OperationalError, ProgrammingError


class ConnectionFailedError(Exception):
    pass


class QueryFailedError(Exception):
    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


@dataclass(frozen=True)
class SfDsn:
    user: str
    password: str | None
    account: str
    database: str


def parse_sf_dsn(dsn: str) -> SfDsn:
    """Parse `snowflake://user:pw@account/database` (spec §7:
    `snowflake://user:pw@account/db/schema.orders` — cli/spec.py already
    strips the `/schema.table` suffix before this module ever sees the
    DSN, same as every other connector). `account` is Snowflake's own
    account identifier (e.g. `abc12345.ap-south-1` or
    `orgname-accountname`), not a host:port pair — urlsplit's `.hostname`
    lower-cases it, which is safe since Snowflake account identifiers are
    themselves case-insensitive.
    """
    parts = urlsplit(dsn)
    if parts.scheme != "snowflake":
        raise ValueError(f"not a snowflake DSN: {dsn!r}")
    database = parts.path.lstrip("/")
    if not database:
        raise ValueError(f"snowflake DSN missing a database: {dsn!r}")
    if not parts.hostname:
        raise ValueError(f"snowflake DSN missing an account identifier: {dsn!r}")
    return SfDsn(
        user=unquote(parts.username) if parts.username else "",
        password=unquote(parts.password) if parts.password else None,
        account=parts.hostname,
        database=database,
    )


def _reraise_as_tablediff_error(e: Exception, sql: str) -> None:
    # DatabaseError covers both a genuinely unreachable account and bad
    # credentials (snowflake-connector-python raises DatabaseError, not a
    # transport-level exception, for a rejected login — confirmed against
    # the real trial account: an incorrect password raises
    # ProgrammingError(250001, "Incorrect username or password was
    # specified") at connect() time, which IS a ProgrammingError
    # subtype-wise but only ever occurs during connect(), where it always
    # means "could not establish a session" — hence connect()'s own
    # wrapping in open_connection() routes every exception through here
    # with the same "<connect>" sentinel `sql`, and OperationalError is
    # kept alongside for a genuine network-level failure (DNS, refused
    # connection, timeout).
    if isinstance(e, (OperationalError, DatabaseError)):
        raise ConnectionFailedError(str(e)) from e
    if sql == "<connect>" and isinstance(e, ProgrammingError):
        raise ConnectionFailedError(str(e)) from e
    if isinstance(e, ProgrammingError):
        raise QueryFailedError(str(e), sql) from e
    raise e


def open_connection(dsn: SfDsn, timeout: float | None = 30.0):
    """Open and return a single persistent connection — the one this side
    of a diff uses for every query until `close_connection()`. `schema`
    defaults to PUBLIC (spec §7's `snowflake://user:pw@account/db/schema.
    orders` example; PUBLIC is Snowflake's own default schema, matching
    Postgres's "public" default in cli/spec.py's grammar) — the actual
    per-table schema a query targets comes from `TableRef.schema` at
    query-build time, not from the connection's default schema, so this
    is just a safe default session context, never load-bearing.
    """
    try:
        return snowflake.connector.connect(
            user=dsn.user,
            password=dsn.password or "",
            account=dsn.account,
            database=dsn.database,
            schema="PUBLIC",
            login_timeout=max(1, int(timeout)) if timeout else 30,
        )
    except Exception as e:  # noqa: BLE001 - re-raised as a typed tablediff error below
        _reraise_as_tablediff_error(e, "<connect>")
        raise  # pragma: no cover - _reraise_as_tablediff_error always raises


def close_connection(conn) -> None:
    conn.close()


def run_query_on(conn, sql: str) -> list[tuple]:
    """Run `sql` on an already-open persistent connection and return rows
    as tuples. This is the hot path every per-segment query in a diff goes
    through — no new connection, no re-authentication. Never mutates
    `sql` (no added LIMIT, no query rewriting) — the exact text a caller
    built is the exact text sent, matching spec §5's `--verbose`-
    reproducibility promise.
    """
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            return cur.fetchall()
        finally:
            cur.close()
    except Exception as e:  # noqa: BLE001
        _reraise_as_tablediff_error(e, sql)
        raise  # pragma: no cover


def check_connection(dsn: SfDsn) -> None:
    try:
        run_query(dsn, "SELECT 1")
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e


def run_query(dsn: SfDsn, sql: str, timeout: float | None = 30.0) -> list[tuple]:
    """Open a fresh connection, run `sql`, return rows as tuples, close.

    A deliberate one-shot path — for `check_connection`'s stateless
    reachability probes and test-only admin operations, never for the
    diff hot path (see module docstring; `SnowflakeConnector.query()` uses
    `run_query_on()` on its one persistent connection instead).
    """
    conn = open_connection(dsn, timeout)
    try:
        return run_query_on(conn, sql)
    finally:
        conn.close()
