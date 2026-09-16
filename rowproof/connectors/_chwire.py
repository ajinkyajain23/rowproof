"""ClickHouse wire access, via `clickhouse-connect` — the real spec §3
driver.

`ClickHouseConnector` in clickhouse.py calls only the functions below,
never clickhouse_connect directly. This module used to be a hand-rolled
stdlib `urllib` HTTP client (see git history / docs/DEV_ENVIRONMENT.md
for why); clickhouse-connect replaces that with the real driver, which
speaks the same HTTP interface but also handles response decoding,
per-engine type mapping, and connection re-use itself instead of this
module doing it by hand.

Two query paths, matching two different needs (mirrors _pgwire.py's own
split — see that module's docstring for the full performance rationale):

* `open_client()` / `run_query_on()` / `close_client()` — a genuinely
  persistent client, opened once by `connect()` and reused for every
  query for the lifetime of a diff (spec §5: "One connection per side.
  No connection pooling in v1."). Reconnecting per query measured at
  ~20ms of pure overhead against a local ClickHouse — negligible for one
  query, ruinous across the thousands a large diff issues.
* `run_query()` / `check_connection()` — a one-shot query against a
  fresh client, for stateless reachability probes and test-only admin
  operations that have no diff-lifetime client to reuse.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

import clickhouse_connect
from clickhouse_connect.driver.client import Client
from clickhouse_connect.driver.exceptions import ClickHouseError
from clickhouse_connect.driver.exceptions import OperationalError as ChOperationalError

# clickhouse-connect logs a warning (not an exception) when a DDL
# response's X-ClickHouse-Summary header isn't the JSON shape a SELECT
# response has (e.g. CREATE DATABASE) — harmless (the statement still
# succeeds), but noisy by default; quiet it to genuine errors only.
logging.getLogger("clickhouse_connect").setLevel(logging.ERROR)

# ClickHouse's AUTHENTICATION_FAILED error code (see
# https://github.com/ClickHouse/ClickHouse/blob/master/src/Common/ErrorCodes.cpp).
# Unlike Postgres, a bad password over ClickHouse's HTTP interface doesn't
# fail at the transport layer (no OperationalError) — the server answers
# with an ordinary-looking error response carrying this code, so it has to
# be recognised by code, not by exception type, to be treated as a
# connection failure rather than a query failure (spec §10: "Clean error
# for: bad credentials").
_AUTH_FAILED_CODE = 516


class ConnectionFailedError(Exception):
    pass


class QueryFailedError(Exception):
    def __init__(self, message: str, sql: str):
        super().__init__(message)
        self.sql = sql


@dataclass(frozen=True)
class ChDsn:
    host: str
    port: int
    user: str
    password: str | None
    database: str


def parse_ch_dsn(dsn: str) -> ChDsn:
    """Parse `clickhouse://user:pw@host:port/database`. Default port 8123
    (ClickHouse's plain-HTTP port — what clickhouse-connect itself talks;
    8443 for HTTPS is out of scope for v1, same as Postgres's DSN handling
    not covering `sslmode`)."""
    parts = urlsplit(dsn)
    if parts.scheme not in ("clickhouse", "ch"):
        raise ValueError(f"not a clickhouse DSN: {dsn!r}")
    database = parts.path.lstrip("/")
    return ChDsn(
        host=parts.hostname or "localhost",
        port=parts.port or 8123,
        user=unquote(parts.username) if parts.username else "default",
        password=unquote(parts.password) if parts.password else None,
        database=database or "default",
    )


def _reraise_as_rowproof_error(e: Exception, sql: str) -> None:
    if isinstance(e, ChOperationalError):
        # A real transport-level failure: refused/unreachable host,
        # connection reset, DNS failure.
        raise ConnectionFailedError(str(e)) from e
    if isinstance(e, ClickHouseError) and getattr(e, "code", None) == _AUTH_FAILED_CODE:
        raise ConnectionFailedError(str(e)) from e
    if isinstance(e, ClickHouseError):
        raise QueryFailedError(str(e), sql) from e
    raise e


def open_client(dsn: ChDsn, timeout: float | None = 10.0) -> Client:
    """Open and return a single persistent client — the one this side of
    a diff uses for every query until `close_client()`."""
    try:
        return clickhouse_connect.get_client(
            host=dsn.host,
            port=dsn.port,
            username=dsn.user,
            password=dsn.password or "",
            database=dsn.database,
            connect_timeout=max(1, int(timeout)) if timeout else 10,
        )
    except Exception as e:  # noqa: BLE001 - re-raised as a typed rowproof error below
        _reraise_as_rowproof_error(e, "<connect>")
        raise  # pragma: no cover - _reraise_as_rowproof_error always raises


def close_client(client: Client) -> None:
    client.close()


def run_query_on(client: Client, sql: str, timeout: float | None = None) -> list[tuple]:
    """Run `sql` on an already-open persistent client and return rows as
    tuples. This is the hot path every per-segment query in a diff goes
    through — no new client, no re-authentication. Never mutates `sql`
    (no FORMAT clause appended, no query rewriting) — the exact text a
    caller built is the exact text sent, matching spec §5's
    `--verbose`-reproducibility promise.
    """
    try:
        settings = {"max_execution_time": timeout} if timeout else None
        result = client.query(sql, settings=settings)
        return [tuple(row) for row in result.result_rows]
    except Exception as e:  # noqa: BLE001
        _reraise_as_rowproof_error(e, sql)
        raise  # pragma: no cover


def check_connection(dsn: ChDsn) -> None:
    try:
        run_query(dsn, "SELECT 1")
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e


def run_query(dsn: ChDsn, sql: str, timeout: float | None = 30.0) -> list[tuple]:
    """Open a fresh client, run `sql`, return rows as tuples, close.

    A deliberate one-shot path — for `check_connection`'s stateless
    reachability probes and test-only admin operations, never for the
    diff hot path (see module docstring; `ClickHouseConnector.query()`
    uses `run_query_on()` on its one persistent client instead).
    """
    client = open_client(dsn, timeout)
    try:
        return run_query_on(client, sql, timeout)
    finally:
        client.close()
