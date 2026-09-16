"""The Connector interface, verbatim per spec §5.

`core/` depends only on this Protocol — never on a concrete connector — so
the algorithm is testable with a fake and portable to any engine that
implements this shape.
"""

from __future__ import annotations

from typing import Protocol

from rowproof.core.models import Column, NormalisationRule, NormaliseOptions, TableRef


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

    # Key-comparison SQL generators — spec §6.3's "Segment boundaries are
    # computed in canonical form and translated back to native literals
    # per engine by the connector," extended to cover *how* a key gets
    # compared/selected in the first place. Added for M2 (real
    # ClickHouse), for the same reason `options` was added to
    # `normalise_expr` above: core/hashdiff.py's segment-and-bisect path
    # and core/joindiff.py's outer join both need engine-specific SQL for
    # a key column that spec §5's literal method list has no slot for,
    # and every connector besides Postgres/ClickHouse can implement all
    # four by just returning the raw quoted column — see
    # `tests/unit/fake_connector.py`'s FakeConnector for the simplest
    # real implementation.
    def key_order_expr(self, column: Column, numeric: bool) -> str:
        """SQL expression to compare/order by this key column when
        building a bounds, segment-boundary WHERE, or sample ORDER BY
        clause. Return the RAW quoted column whenever comparing it
        directly is provably safe — an indexed range scan is the whole
        point of segmentation, and wrapping the column in an expression
        (a CAST, or a COLLATE that doesn't match the index's own
        collation) can stop the query planner from using that index at
        all. Force a different, deterministic comparison only when the
        engine's own default ordering for this column isn't guaranteed
        stable across separate bounds/sample/segment queries (e.g. a
        Postgres text column under a locale-aware default collation)."""
        ...

    def key_bounds_expr(self, column: Column, numeric: bool) -> str:
        """Like `key_order_expr`, but specifically for a MIN/MAX bounds
        aggregate query — some engines need a different expression there
        even when `key_order_expr`'s own expression is a fine ordinary
        comparison target (e.g. an engine with no native MIN/MAX
        aggregate for the key's type, needing a cast just for that
        query). A connector with no such special case can simply return
        `self.key_order_expr(column, numeric)`."""
        ...

    def key_select_expr(self, column: Column) -> str:
        """SQL expression to SELECT this key column so that, after an
        OUTER JOIN (core/joindiff.py), an unmatched row's side is
        guaranteed to come back as genuine SQL NULL there — never a
        type's default value. Most connectors can just return the raw
        quoted column; only needed for an engine whose OUTER JOIN
        implementation fills a non-Nullable column's unmatched side with
        a default instead of NULL."""
        ...

    def sample_sql(self, table_sql: str, key_col: str, sample_cap: int) -> str:
        """SQL selecting up to `sample_cap` random values of the
        already-quoted `key_col` from `table_sql` (a FROM-clause-ready
        table reference or parenthesised subquery, already filtered by
        any `--where`) — used only for non-numeric key segmentation's
        quantile-boundary sampling. The caller always re-sorts the
        result with `key_order_expr()` itself; this method's own row
        order is never trusted."""
        ...
