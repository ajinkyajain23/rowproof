"""Postgres wire access — STAND-IN IMPLEMENTATION.

The spec (§3) calls for `psycopg` (v3), talking the Postgres wire protocol
directly. This dev environment has no network access to install it (see
docs/DEV_ENVIRONMENT.md), so this module shells out to the `psql` CLI
binary instead, which the base image already has installed.

This is the ONLY file that should need to change when psycopg becomes
available — `PostgresConnector` in postgres.py calls only `run_query()`
and `check_connection()` below, never subprocess directly.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

FIELD_SEP = "\x1f"
RECORD_SEP = "\x1e"
NULL_SENTINEL = "\x02TABLEDIFF-NULL\x02"


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


def _base_cmd(dsn: PgDsn) -> list[str]:
    return [
        "psql",
        "-X",  # ignore ~/.psqlrc
        "-q",  # quiet
        "-A",  # unaligned
        "-t",  # tuples only, no headers
        "-F", FIELD_SEP,
        "-R", RECORD_SEP,
        "-P", f"null={NULL_SENTINEL}",
        "-v", "ON_ERROR_STOP=1",
        "-h", dsn.host,
        "-p", str(dsn.port),
        "-U", dsn.user,
        "-d", dsn.database,
    ]


def _env_for(dsn: PgDsn) -> dict:
    import os

    env = os.environ.copy()
    # Password travels via env var, never as a CLI arg, so it can never
    # show up in `ps aux` output or in a logged command line — the same
    # guarantee psycopg gives you, just achieved differently (spec §7:
    # "Never print passwords, even at --verbose; redact before logging").
    if dsn.password is not None:
        env["PGPASSWORD"] = dsn.password
    else:
        env.pop("PGPASSWORD", None)
    return env


def _smart_cast(value: str):
    """psql always returns text; cast back to int where it's unambiguous.

    core/segmentation.py does real arithmetic (min/max/span) on numeric
    key bounds, so those need to come back as Python ints, exactly like a
    real DB-API driver would hand back. Everything else stays a string —
    core never needs floats/decimals in M0, and leaving text as text is
    always safe.

    "Unambiguous" is doing real work here: a *text* column can hold a
    value that merely looks numeric (a zip code, an external ID with a
    leading zero) and this stand-in has no column-type metadata to tell
    the difference — a real driver would. The one signal we do have is
    spec §6.1's own INT-1 canonical form: "decimal digits, no leading
    zeros". A value with a leading zero (other than the literal "0") is
    therefore never a genuine Postgres integer render, so we leave it as
    text rather than silently mangling it (e.g. '02139' staying '02139',
    not becoming 2139). This narrows the ambiguity; it can't close it
    completely — a text column containing exactly '42' is still
    indistinguishable from a real integer over a text-only protocol. That
    residual gap is exactly why the real fix is switching to psycopg
    (see docs/DEV_ENVIRONMENT.md), which knows each column's actual type.
    """
    if value == "" or value is None:
        return value
    body = value[1:] if value and value[0] in "+-" else value
    if body.isdigit() and (body == "0" or body[0] != "0"):
        return int(value)
    return value


def check_connection(dsn: PgDsn, timeout: float = 10.0) -> None:
    try:
        run_query(dsn, "SELECT 1", timeout=timeout)
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e


def run_query(dsn: PgDsn, sql: str, timeout: float | None = 60.0) -> list[tuple]:
    cmd = [*_base_cmd(dsn), "-c", sql]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            env=_env_for(dsn),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise QueryFailedError(f"query timed out after {timeout}s", sql) from e
    except FileNotFoundError as e:
        raise ConnectionFailedError("psql binary not found on PATH") from e

    if proc.returncode != 0:
        stderr = proc.stderr.strip()
        # psql's real wording for a dropped/refused connection is
        # "connection to server ... failed: Connection refused" or "...
        # failed: server closed the connection unexpectedly" — match on
        # "connection ... failed" in the message itself, not on the
        # command name (cmd[0] is always literally "psql").
        connection_markers = (
            "could not connect",
            "connection to server",
            "could not translate host name",
            "server closed the connection",
            "terminating connection",
            "SSL SYSCALL error",
        )
        if any(marker in stderr for marker in connection_markers):
            raise ConnectionFailedError(stderr)
        if "authentication failed" in stderr or "password authentication failed" in stderr:
            raise ConnectionFailedError(stderr)
        raise QueryFailedError(stderr, sql)

    raw = proc.stdout
    if raw.endswith("\n"):
        raw = raw[:-1]
    if raw == "":
        return []

    rows = []
    for record in raw.split(RECORD_SEP):
        fields = record.split(FIELD_SEP)
        rows.append(tuple(None if f == NULL_SENTINEL else _smart_cast(f) for f in fields))
    return rows
