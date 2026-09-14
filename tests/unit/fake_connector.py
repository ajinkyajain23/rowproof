"""A real-SQL fake Connector, backed by Python's stdlib sqlite3.

Spec §0 rule 4 requires core/ to be "testable ... with fake connectors."
Rather than hand-roll a partial SQL interpreter, this fake registers a
couple of Python functions into an in-memory SQLite database and then lets
core/hashdiff.py generate and execute *real* SQL against it — exactly the
same contract a Postgres/ClickHouse connector fulfils, just aimed at a
different engine. This means the orchestration logic (segmentation,
bisection, row classification, capping) is exercised through genuine query
execution in unit tests, with no network and no external dependency.

It deliberately does NOT try to byte-for-byte match Postgres's MD5-derived
hash (that cross-engine equality is a connector-level contract tested in
M2 against real Postgres + ClickHouse) — only that the aggregate is
order-independent and collision-free enough for test fixtures.
"""

from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass, field

from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, TableRef


def _row_hash(canonical: str) -> int:
    digest = hashlib.md5(canonical.encode("utf-8")).digest()
    val = int.from_bytes(digest[:8], "big", signed=False)
    # Fold into signed 64-bit range like Postgres's ::bit(64)::bigint cast.
    if val >= 2**63:
        val -= 2**64
    return val


class _XorAgg:
    def __init__(self):
        self.acc = 0

    def step(self, value):
        if value is None:
            return
        # work in unsigned 64-bit space for a well-defined XOR
        self.acc ^= value & 0xFFFFFFFFFFFFFFFF

    def finalize(self):
        val = self.acc
        if val >= 2**63:
            val -= 2**64
        return val


@dataclass
class _TableMeta:
    columns: list[Column]
    primary_key: list[str] | None


@dataclass
class FakeConnector:
    engine: str = "fake"
    _conn: sqlite3.Connection | None = None
    _tables: dict[str, _TableMeta] = field(default_factory=dict)

    def connect(self, dsn: str) -> None:
        self._conn = sqlite3.connect(":memory:")
        self._conn.create_function("tdhash", 1, _row_hash, deterministic=True)
        self._conn.create_aggregate("tdxor", 1, _XorAgg)
        # key_order_expr() below emits `COLLATE "C"` around every
        # non-numeric key comparison (a fixed, locale-independent
        # byte-order compare) so bounds/sample/segment queries all agree
        # on one ordering. Postgres ships "C" built in;
        # sqlite doesn't know that name unless something registers it, so
        # register one here that does the same plain byte-order compare
        # Python's default string comparison already gives us — this way
        # the fake executes the *exact same SQL shape* a real connector
        # would, not a hand-waved substitute.
        self._conn.create_collation("C", lambda a, b: (a > b) - (a < b))

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # --- test setup helpers (not part of the Connector protocol) -------

    def create_table(
        self,
        name: str,
        columns: list[Column],
        primary_key: list[str] | None,
        rows: list[dict],
    ) -> None:
        assert self._conn is not None, "call connect() first"
        col_defs = ", ".join(f'"{c.name}"' for c in columns)
        self._conn.execute(f'CREATE TABLE "{name}" ({col_defs})')
        placeholders = ", ".join("?" for _ in columns)
        col_names = [c.name for c in columns]
        for row in rows:
            values = [row[c] for c in col_names]
            self._conn.execute(
                f'INSERT INTO "{name}" VALUES ({placeholders})', values
            )
        self._conn.commit()
        self._tables[name] = _TableMeta(columns=columns, primary_key=primary_key)

    # --- Connector protocol ---------------------------------------------

    def get_schema(self, table: TableRef) -> list[Column]:
        return list(self._tables[table.table].columns)

    def get_primary_key(self, table: TableRef) -> list[str] | None:
        pk = self._tables[table.table].primary_key
        return list(pk) if pk else None

    def query(self, sql: str, params: dict | None = None) -> list[tuple]:
        assert self._conn is not None, "call connect() first"
        cur = self._conn.execute(sql, params or {})
        return cur.fetchall()

    def normalise_expr(
        self,
        column: Column,
        rule: NormalisationRule,
        options: NormaliseOptions | None = None,
    ) -> str:
        # `options` is accepted (per the Connector protocol, see
        # connectors/base.py) but not applied to any rendering here — every
        # fixture-level, options-sensitive rule (DEC-1/FLT-1/STR-2/TS-2/
        # TS-3) is verified against real Postgres in
        # tests/integration/test_m1_fixtures.py, per spec §13's "verify on
        # real databases" instruction. This fake only needs to keep
        # exercising core/hashdiff.py's orchestration (segmentation,
        # bisection, row classification) with the M0-era rules.
        quoted = self.quote_identifier(column.name)
        if rule is NormalisationRule.INT_1:
            inner = f"CAST({quoted} AS TEXT)"
        elif rule is NormalisationRule.TS_1:
            # Fixtures store timestamps already as canonical ISO-8601 UTC
            # text, matching what a real connector's TS-1 SQL would yield.
            inner = f"CAST({quoted} AS TEXT)"
        else:  # STR_1, UNK_1 and anything else: text as-is
            inner = f"CAST({quoted} AS TEXT)"
        if column.nullable:
            return f"(CASE WHEN {quoted} IS NULL THEN '\\N' ELSE {inner} END)"
        return inner

    def row_hash_expr(self, exprs: list[str]) -> str:
        concat = " || CHAR(31) || ".join(exprs) if len(exprs) > 1 else exprs[0]
        return f"tdhash({concat})"

    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        return f"tdxor({row_hash_expr})"

    def key_order_expr(self, column: Column, numeric: bool) -> str:
        q = self.quote_identifier(column.name)
        if numeric:
            return q
        if column.native_type.strip().lower() == "uuid":
            return q
        # This fake's sqlite backing registers a "C" collation
        # specifically so its unit tests keep exercising the real
        # forced-`COLLATE "C"` SQL shape a locale-aware Postgres column
        # would need (see connect()'s own comment) — `column.collation`
        # is always None here (this fake never reports one), so this
        # always takes that branch, matching PostgresConnector's own
        # fallback for an unrecognised/absent collation.
        return f'{q} COLLATE "C"'

    def key_bounds_expr(self, column: Column, numeric: bool) -> str:
        return self.key_order_expr(column, numeric)

    def key_select_expr(self, column: Column) -> str:
        return self.quote_identifier(column.name)

    def sample_sql(self, table_sql: str, key_col: str, sample_cap: int) -> str:
        # sqlite understands RANDOM() natively (one of the few spellings
        # it and Postgres happen to share).
        q = self.quote_identifier(key_col)
        return f"SELECT {q} FROM {table_sql} ORDER BY RANDOM() LIMIT {sample_cap}"

    def quote_identifier(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def quote_literal(self, value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("'", "''")
        return f"'{escaped}'"
