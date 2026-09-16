"""The cross-engine hash-equality test (spec §5: "add a cross-engine test
that hashes the same fixture string on every engine and asserts equality.
That test is non-negotiable.") — user's explicit instruction: this is the
first thing M2 writes.

*** WHAT THIS TEST ACTUALLY PROVES, AND WHAT IT DOESN'T ***
This environment has no reachable ClickHouse (see clickhouse.py and
_chwire.py's module docstrings for the full story — no Docker, no
network access to install one, the linked desktop hit an unrelated
Windows-bridge bug). So this file does NOT run ClickHouse's SQL. What it
DOES do: runs Postgres's row_hash_expr for real (a real psql round-trip —
this part is fully verified), and separately replicates, step by step in
pure Python, exactly what ClickHouseConnector.row_hash_expr's generated
SQL (MD5 -> hex -> lower -> substr(1,16) -> unhex -> reverse ->
reinterpretAsInt64) computes according to documented ClickHouse function
semantics. It asserts those two independently-arrived-at values are equal
for every fixture string.

That is real evidence the byte-order reasoning in
ClickHouseConnector.row_hash_expr's docstring is internally consistent —
but it is NOT the spec's actual required test, which must execute the
real generated SQL against a real ClickHouse server. See
tests/integration/test_clickhouse_real.py for that test, gated behind a
reachable server this environment doesn't have. Until that runs green,
M2's cross-engine hash-equality acceptance criterion (spec §13) is
UNVERIFIED, not passing.
"""

from __future__ import annotations

import hashlib
import os

import pytest

from rowproof.connectors import _pgwire

PG_ADMIN_DSN_URL = os.environ.get(
    "ROWPROOF_TEST_PG_ADMIN_DSN", "postgres://postgres:postgres@127.0.0.1:5432/postgres"
)


def _postgres_row_hash(fixture_string: str) -> int:
    """The REAL value: runs Postgres's actual row_hash_expr SQL
    (('x' || substr(md5(s),1,16))::bit(64)::bigint) against a live
    server."""
    dsn = _pgwire.parse_pg_dsn(PG_ADMIN_DSN_URL)
    quoted = "'" + fixture_string.replace("'", "''") + "'"
    sql = f"SELECT ('x' || substr(md5({quoted}),1,16))::bit(64)::bigint"
    rows = _pgwire.run_query(dsn, sql)
    return rows[0][0]


def _clickhouse_row_hash_reference(fixture_string: str) -> int:
    """A pure-Python replica of what ClickHouseConnector.row_hash_expr's
    generated SQL computes, per documented ClickHouse function semantics:

    MD5(s)                          -> the raw 16-byte digest
    hex(...)                        -> uppercase hex string of those bytes
    lower(...)                      -> lowercased
    substr(...,1,16)                -> first 16 hex chars (= first 8 bytes)
    unhex(...)                      -> those 8 bytes, raw, in original order
    reverse(...)                    -> same 8 bytes, order reversed
    reinterpretAsInt64(...)         -> read as a LITTLE-endian signed int64

    Reading the reversed bytes little-endian is exactly equivalent to
    reading the original bytes big-endian — implemented directly here
    (not via a shortcut) so this function's own logic mirrors the SQL
    step for step and is auditable against it.
    """
    digest = hashlib.md5(fixture_string.encode("utf-8")).digest()
    first8 = digest[:8]
    reversed8 = first8[::-1]
    return int.from_bytes(reversed8, byteorder="little", signed=True)


FIXTURE_STRINGS = [
    "hello",
    "",
    "row-2",  # empirically confirmed negative case (top bit set) — see below
    "2024-03-01T04:30:00.000000Z",
    "\\N",  # the NULL-1 marker itself, hashed as an ordinary string here
    "12.50",
    "a" * 200,
    "unicode: café ☃",
]


@pytest.fixture(scope="module", autouse=True)
def _require_postgres():
    try:
        _pgwire.check_connection(_pgwire.parse_pg_dsn(PG_ADMIN_DSN_URL))
    except Exception as e:  # noqa: BLE001
        pytest.exit(
            f"Postgres is not reachable at {PG_ADMIN_DSN_URL!r} — this test needs it "
            f"as the real side of the cross-engine comparison ({e}).",
            returncode=1,
        )


class TestCrossEngineHashReference:
    @pytest.mark.parametrize("fixture_string", FIXTURE_STRINGS)
    def test_postgres_and_simulated_clickhouse_agree(self, fixture_string):
        pg_value = _postgres_row_hash(fixture_string)
        ch_value = _clickhouse_row_hash_reference(fixture_string)
        assert pg_value == ch_value, (
            f"byte-order reasoning in ClickHouseConnector.row_hash_expr's docstring "
            f"does not hold for {fixture_string!r}: postgres={pg_value} vs "
            f"simulated-clickhouse={ch_value}"
        )

    def test_regression_values_match_the_empirically_confirmed_psql_round_trip(self):
        # Pinned to the exact values confirmed via two real `psql` queries
        # during development (see clickhouse.py's row_hash_expr docstring)
        # — if these ever drift, something upstream (hashlib, the SQL
        # itself) changed meaning, not just this test's expectations.
        assert _postgres_row_hash("hello") == 6719722671305337462
        assert _postgres_row_hash("row-2") == -1091569353222381949
        assert _clickhouse_row_hash_reference("hello") == 6719722671305337462
        assert _clickhouse_row_hash_reference("row-2") == -1091569353222381949

    def test_byte_order_actually_matters_here(self):
        """Guards against this test suite being vacuously true because
        reversing bytes happens to be a no-op for MD5 digests in general
        — it isn't: the naive (non-reversed) little-endian read must
        differ from Postgres's big-endian read for at least one fixture,
        proving the reverse() step in row_hash_expr is doing real work,
        not a decoration."""
        digest = hashlib.md5(b"hello").digest()
        naive_little_endian = int.from_bytes(digest[:8], byteorder="little", signed=True)
        pg_value = _postgres_row_hash("hello")
        assert naive_little_endian != pg_value
