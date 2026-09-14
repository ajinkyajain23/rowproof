"""Joindiff algorithm — spec §4.2: same engine, same database, a single
FULL OUTER JOIN query does the whole comparison. Faster and exact where
hashdiff's segment-and-bisect approach is needed only because the two
tables *can't* be queried together.

Reuses core/hashdiff.py's plan-building, column-matching and uniqueness-
check machinery (same _Plan shape, same §6.2 column matching) rather than
duplicating it — the two algorithms differ only in how they turn a
matched, planned comparison into SQL and rows.
"""

from __future__ import annotations

import time

from tablediff.connectors.base import Connector
from tablediff.core.hashdiff import (
    DEFAULT_MAX_DIFF_ROWS,
    _apply_column_match,
    _build_plan,
    _maybe_append,
    _quoted_table,
    _validate_key_unique_side,
    _Plan,
)
from tablediff.core.models import (
    Algorithm,
    DiffResult,
    NormaliseOptions,
    RowDiff,
    TableRef,
    Warning as TdWarning,
)


def _side_subquery_sql(plan: _Plan, alias: str) -> str:
    """One side's rows, pre-normalised inside its own subquery scope —
    critical so normalise_expr's bare `quote_identifier(column.name)`
    stays unambiguous (a FULL OUTER JOIN of two same-shaped tables makes
    every column name ambiguous *outside* this subquery, since both sides
    have a same-named column; inside here only one table is visible)."""
    connector = plan.connector
    quote = connector.quote_identifier
    key_select = [quote(k) for k in plan.key_columns]
    value_select = [
        f"{connector.normalise_expr(plan.columns_by_name[c], plan.rule_for(c), plan.options)} AS {quote(c)}"
        for c in plan.value_columns
    ]
    select_list = ", ".join([*key_select, *value_select])
    base = _quoted_table(connector, plan.table)
    where_clause = f" WHERE {plan.where}" if plan.where else ""
    return f"(SELECT {select_list} FROM {base}{where_clause}) {alias}"


class _JoinShape:
    """The FROM/JOIN/WHERE that every diff-detecting query over this pair
    shares — factored out so the bounded row-detail query and the exact
    aggregate-count query (see `_join_sql` / `_diff_counts_sql`) can never
    drift apart and silently start counting a different set of rows than
    they list.
    """

    def __init__(self, src_plan: _Plan, tgt_plan: _Plan) -> None:
        connector = src_plan.connector  # both sides run through the one shared connection
        quote = connector.quote_identifier
        key_cols = src_plan.key_columns
        self.value_cols = src_plan.value_columns
        self.first_key = quote(key_cols[0])
        self.quote = quote

        self.src_sub = _side_subquery_sql(src_plan, "s")
        self.tgt_sub = _side_subquery_sql(tgt_plan, "t")
        self.join_cond = " AND ".join(f"s.{quote(k)} = t.{quote(k)}" for k in key_cols)
        self.key_exprs = [f"COALESCE(s.{quote(k)}, t.{quote(k)})" for k in key_cols]

        diff_conds = " OR ".join(f"s.{quote(c)} IS DISTINCT FROM t.{quote(c)}" for c in self.value_cols)
        where_parts = [f"s.{self.first_key} IS NULL", f"t.{self.first_key} IS NULL"]
        if diff_conds:
            where_parts.append(f"({diff_conds})")
        self.where_clause = " OR ".join(where_parts)

    @property
    def from_clause(self) -> str:
        return f"{self.src_sub} FULL OUTER JOIN {self.tgt_sub} ON {self.join_cond}"


def _join_sql(shape: _JoinShape, limit: int | None = None) -> str:
    """The bounded row-detail query: up to `limit` differing rows, in key
    order — spec's "cap collecting row-level differences" applies to what
    the CLIENT holds, so the cap belongs in the SQL (`LIMIT`), not in a
    Python loop that has already pulled every row across the wire before
    discarding the excess.
    """
    quote = shape.quote
    value_pairs = []
    for c in shape.value_cols:
        q = quote(c)
        value_pairs.extend([f"s.{q}", f"t.{q}"])

    # A row missing a source counterpart (s.<key> IS NULL) is one that
    # exists only in the target — "extra in target" — and symmetrically a
    # row with no target counterpart is "missing in target". Ordered here
    # as (missing_flag, extra_flag) to match how diff() unpacks them.
    select_list = ", ".join(
        [*shape.key_exprs, *value_pairs, f"(t.{shape.first_key} IS NULL)", f"(s.{shape.first_key} IS NULL)"]
    )
    order_by = ", ".join(shape.key_exprs)
    sql = f"SELECT {select_list} FROM {shape.from_clause} WHERE {shape.where_clause} ORDER BY {order_by}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return sql


def _diff_counts_sql(shape: _JoinShape) -> str:
    """Exact missing/extra/changed counts in ONE query, over the same
    FROM/JOIN/WHERE `_join_sql` uses — so the row-detail query can be
    bounded with `LIMIT` (spec §4.1 step 6's "counts stay exact; row
    lists don't" applies just as much to joindiff) without losing the
    exact totals to that same limit.
    """
    return (
        f"SELECT "
        f"COUNT(*) FILTER (WHERE t.{shape.first_key} IS NULL), "
        f"COUNT(*) FILTER (WHERE s.{shape.first_key} IS NULL), "
        f"COUNT(*) FILTER (WHERE s.{shape.first_key} IS NOT NULL AND t.{shape.first_key} IS NOT NULL) "
        f"FROM {shape.from_clause} WHERE {shape.where_clause}"
    )


def _is_true(value) -> bool:
    """Normalise a boolean flag column back to a real Python bool.

    Three different "truthy" shapes show up here depending on which
    Connector is under test: a real driver (psycopg) hands back an
    actual bool; _pgwire.py's psql-backed stand-in returns Postgres
    boolean output as the literal text "t"/"f" (see its own _smart_cast
    docstring — it only special-cases digit strings back to int, never
    booleans) — and a non-empty string like "f" is truthy in plain
    Python; sqlite (tests/unit/fake_connector.py) has no boolean type at
    all and returns `IS NULL`'s result as int 0/1. Handle all three
    explicitly rather than trusting bare Python truthiness on whatever
    the connector happened to hand back.
    """
    if isinstance(value, bool):
        return value
    return value in (1, "t")


def _count_sql(plan: _Plan) -> str:
    connector = plan.connector
    base = _quoted_table(connector, plan.table)
    where_clause = f" WHERE {plan.where}" if plan.where else ""
    return f"SELECT COUNT(*) FROM {base}{where_clause}"


def diff(
    source: Connector,
    target: Connector,
    source_table: TableRef,
    target_table: TableRef,
    key_columns: list[str] | None = None,
    columns: list[str] | None = None,
    exclude: list[str] | None = None,
    max_diff_rows: int = DEFAULT_MAX_DIFF_ROWS,
    where: str | None = None,
    where_source: str | None = None,
    where_target: str | None = None,
    trim: bool = False,
    case_insensitive: bool = False,
    float_precision: int = 15,
    assume_tz: str = "UTC",
    column_map: dict | None = None,
) -> DiffResult:
    """Precondition (spec §4.2, enforced by the CLI's algorithm resolution
    in cli/main.py, not re-checked here): `source` and `target` are two
    Connector objects pointed at the *same* underlying connection/database
    — joindiff issues its one query through `source`, so if that
    precondition doesn't hold this simply can't see `target_table`'s rows
    and will fail with a normal "relation does not exist" style error
    from the engine, not a silent wrong answer.
    """
    start_time = time.monotonic()
    src_where = where_source or where
    tgt_where = where_target or where
    src_plan = _build_plan(source, source_table, key_columns, columns, exclude, src_where)
    tgt_plan = _build_plan(target, target_table, key_columns, columns, exclude, tgt_where)

    options = NormaliseOptions(
        trim=trim, case_insensitive=case_insensitive,
        float_precision=float_precision, assume_tz=assume_tz,
    )
    result = DiffResult(
        source=source_table,
        target=target_table,
        key_columns=tuple(src_plan.key_columns),
        algorithm=Algorithm.JOINDIFF,
    )
    _apply_column_match(src_plan, tgt_plan, result, options, column_map)

    _validate_key_unique_side(src_plan)
    _validate_key_unique_side(tgt_plan)

    result.source_count = source.query(_count_sql(src_plan))[0][0]
    result.target_count = target.query(_count_sql(tgt_plan))[0][0]
    if result.source_count != result.target_count:
        result.warnings.append(
            TdWarning(f"row counts differ: source={result.source_count} target={result.target_count}")
        )

    n_key = len(src_plan.key_columns)
    n_value = len(src_plan.value_columns)
    shape = _JoinShape(src_plan, tgt_plan)

    # Exact counts first, from one aggregate query — independent of
    # whatever LIMIT the detail query below applies, so a huge number of
    # real differences never has to pass through Python just to be
    # counted (spec §2's "never pull full tables to the client", and
    # §4.1 step 6's exact-counts-truncated-list contract, both apply here
    # exactly as much as they do to hashdiff).
    missing_count, extra_count, changed_count = source.query(_diff_counts_sql(shape))[0]
    result.missing_in_target = missing_count
    result.extra_in_target = extra_count
    result.changed = changed_count
    total_diffs = missing_count + extra_count + changed_count
    result.truncated = total_diffs > max_diff_rows

    # Bounded row-detail fetch: SQL LIMIT max_diff_rows + 1, one more row
    # than we'll ever store. `truncated` is already exact from the COUNT
    # query above and doesn't depend on this; the "+1" here is a second,
    # independent signal — the detail query can itself observe whether
    # there was another row past the cap, even if the two queries were
    # ever to disagree (e.g. concurrent writes between them). The stored
    # list is still capped at exactly max_diff_rows via _maybe_append,
    # same contract hashdiff uses.
    detail_sql = _join_sql(shape, limit=max_diff_rows + 1)
    rows = source.query(detail_sql)

    for row in rows:
        key = tuple(row[:n_key])
        pair_values = row[n_key:n_key + 2 * n_value]
        is_missing = _is_true(row[-2])
        is_extra = _is_true(row[-1])
        if is_missing:
            _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="missing"))
        elif is_extra:
            _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="extra"))
        else:
            changes = {}
            for i, c in enumerate(src_plan.value_columns):
                sv, tv = pair_values[2 * i], pair_values[2 * i + 1]
                if sv != tv:
                    changes[c] = (sv, tv, src_plan.rule_for(c))
            if changes:
                _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="changed", changes=changes))

    result.segments_examined = 1  # the whole comparison is one query, not segmented
    result.queries_per_side = 1
    result.elapsed_seconds = time.monotonic() - start_time
    return result
