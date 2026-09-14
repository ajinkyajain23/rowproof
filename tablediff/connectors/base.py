"""The Connector interface, verbatim per spec §5.

`core/` depends only on this Protocol — never on a concrete connector — so
the algorithm is testable with a fake and portable to any engine that
implements this shape.
"""

from __future__ import annotations

from typing import Protocol

from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, TableRef


class Connector(Protocol):
    engine: str  # "postgres" | "clickhouse" | "snowflake"

    def connect(self, dsn: str) -> None: ...

    def close(self) -> None: ...

    def get_schema(self, table: TableRef) -> list[Column]:
        """Column name, native type, nullable, ordinal position."""
        ...

    def get_primary_key(self, table: TableRef) -> list[str] | None: ...

    def query(self, sql: str, params: dict | None = None) -> list[tuple]: ...

    # SQL fragment generators — return SQL text, never execute.
    def normalise_expr(
        self,
        column: Column,
        rule: NormalisationRule,
        options: NormaliseOptions | None = None,
    ) -> str:
        """SQL expression that renders this column as the canonical string
        in §6, wrapped for NULL-1 handling.

        `options` is a deliberate, justified extension beyond spec §5's
        literal listing (same precedent as the earlier collation-related
        change): §6.1's STR-2/FLT-1/TS-3 rules are only fully specified
        together with the `--trim`/`--case-insensitive`/`--float-precision`/
        `--assume-tz` CLI flags (§7), and DEC-1/TS-2 need the *pair's*
        min scale/precision (§6.2), which a single column can't know about
        itself. A connector that ignores `options` (or receives None) must
        still render a spec-correct canonical form using the column's own
        declared scale/precision and the rule defaults (float_precision=15,
        assume_tz="UTC", trim/case_insensitive off)."""
        ...

    def row_hash_expr(self, exprs: list[str]) -> str:
        """SQL expression hashing the concatenation of normalised column
        expressions into a 64-bit integer."""
        ...

    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        """Order-independent aggregate over row hashes within a GROUP BY
        (or a bare WHERE-filtered query, in M0's per-segment form)."""
        ...

    def quote_identifier(self, name: str) -> str: ...

    def quote_literal(self, value: object) -> str: ...
