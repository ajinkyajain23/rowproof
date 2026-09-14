"""ClickHouse wire access — the HTTP interface, via the stdlib only.

Spec §3 calls for `clickhouse-connect`. This dev environment has no
network access to install third-party packages (see docs/DEV_ENVIRONMENT.md
and _pgwire.py's identical note for Postgres), and unlike Postgres there is
no `clickhouse-client` CLI binary pre-installed to shell out to either — so
this module is not a "stand-in" the way _pgwire.py's psql-shim is; it's a
real, complete client for ClickHouse's plain HTTP interface (a documented,
stable wire format: POST a SQL string, get back a response body in
whatever FORMAT was requested — no proprietary framing to reimplement).
`clickhouse-connect` itself talks to the very same HTTP interface under
the hood. This is the file to swap if a dedicated driver becomes
installable later; `ClickHouseConnector` in clickhouse.py calls only
`run_query()` and `check_connection()` below, never urllib directly.

*** VERIFICATION STATUS: UNTESTED AGAINST A LIVE CLICKHOUSE SERVER. ***
This environment has no reachable ClickHouse instance (no Docker daemon,
no apt/pip network access to install one, no pre-installed binary — see
the M2 conversation notes). Every line of SQL this project generates for
ClickHouse has been reasoned through against documented ClickHouse
semantics and, where possible, cross-checked against real Postgres output
(see clickhouse.py's row_hash_expr docstring for the one case that
actually matters most: the cross-engine hash must produce the identical
64-bit integer on both engines for the same fixture string). None of it
has been executed against a real server. Spec §13 M2's acceptance
criteria are NOT satisfied and M2 is NOT being reported done until that
real verification happens.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import unquote, urlencode, urlsplit


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
    (ClickHouse's plain-HTTP port; 8443 for HTTPS is out of scope for v1,
    same as Postgres's DSN handling not covering `sslmode`)."""
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


def _base_url(dsn: ChDsn) -> str:
    params = {
        "database": dsn.database,
        "default_format": "JSONCompact",
        # Render 64-bit ints as raw JSON numbers, not quoted strings —
        # ClickHouse's default JSON output quotes Int64/UInt64 to protect
        # JS float precision, which we don't need and would rather not
        # have to un-quote by hand (Python's json module has no such
        # precision limit).
        "output_format_json_quote_64bit_integers": "0",
    }
    return f"http://{dsn.host}:{dsn.port}/?{urlencode(params)}"


def run_query(dsn: ChDsn, sql: str) -> list[tuple]:
    """POST `sql` to ClickHouse's HTTP interface and return rows as
    tuples. Never mutates `sql` (no FORMAT clause appended) — the exact
    text a caller built is the exact text sent, matching spec §5's
    `--verbose`-reproducibility promise; the response FORMAT is chosen via
    the `default_format` URL param instead, which only takes effect when
    the query itself has no explicit FORMAT clause.
    """
    req = urllib.request.Request(
        _base_url(dsn), data=sql.encode("utf-8"), method="POST"
    )
    if dsn.password is not None or dsn.user != "default":
        req.add_header("X-ClickHouse-User", dsn.user)
        req.add_header("X-ClickHouse-Key", dsn.password or "")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="replace")
        raise QueryFailedError(f"ClickHouse query failed ({e.code}): {detail}", sql) from e
    except urllib.error.URLError as e:
        raise ConnectionFailedError(f"cannot reach ClickHouse at {dsn.host}:{dsn.port}: {e.reason}") from e

    if not body.strip():
        return []
    payload = json.loads(body)
    return [tuple(row) for row in payload.get("data", [])]


def check_connection(dsn: ChDsn) -> None:
    try:
        run_query(dsn, "SELECT 1")
    except QueryFailedError as e:
        raise ConnectionFailedError(str(e)) from e
