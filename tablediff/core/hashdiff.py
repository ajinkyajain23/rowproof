"""Hashdiff algorithm — spec §4.1.

Zero database-driver imports (spec §0 rule 4): every SQL string is built
here from the pieces a Connector hands back (normalise_expr, row_hash_expr,
aggregate_hash_expr, quote_identifier, quote_literal) and executed only
through connector.query(). Swap in any object satisfying
tablediff.connectors.base.Connector — real or fake — and this module
doesn't change.
"""

from __future__ import annotations

import dataclasses
import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass

from tablediff.connectors.base import Connector
from tablediff.core.column_matching import match_columns
from tablediff.core.errors import NonUniqueKeyError, NoPrimaryKeyError
from tablediff.core.models import (
    Algorithm,
    Column,
    DiffResult,
    NormalisationRule,
    NormaliseOptions,
    RowDiff,
    Segment,
    TableRef,
    Warning as TdWarning,
)
from tablediff.core.normalisation import pick_rule
from tablediff.core.segmentation import (
    assert_contiguous_coverage,
    clamp_to_range,
    compute_num_segments,
    segment_by_samples,
    segment_numeric_range,
)

DEFAULT_ROW_THRESHOLD = 1000
DEFAULT_MAX_DIFF_ROWS = 10_000
DEFAULT_SAMPLE_CAP = 10_000
_NUMERIC_RULES = {NormalisationRule.INT_1, NormalisationRule.DEC_1, NormalisationRule.FLT_1}


@dataclass
class _Plan:
    """Everything resolved once per side before segment queries start."""

    connector: Connector
    table: TableRef
    columns: list[Column]
    columns_by_name: dict[str, Column]
    key_columns: list[str]
    value_columns: list[str]  # every compared column except the key
    key_rule: NormalisationRule  # rule for key_columns[0] (drives segmentation)
    where: str | None = None  # spec §7 --where / --where-source / --where-target
    # Populated by _apply_column_match() once both sides' schemas are known
    # (spec §6.2) — a fresh _Plan from _build_plan() alone has empty
    # defaults (no rule upgraded, no scale/precision override), which is
    # only correct before matching has run.
    options: NormaliseOptions = None  # type: ignore[assignment]
    rule_overrides: dict = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.options is None:
            self.options = NormaliseOptions()
        if self.rule_overrides is None:
            self.rule_overrides = {}

    def rule_for(self, column_name: str) -> NormalisationRule:
        """The NormalisationRule to render `column_name` with — a §6.2
        pair-level upgrade (TS-2, STR-2) when match_columns() decided one
        applies, else the plain per-column pick_rule() result."""
        override = self.rule_overrides.get(column_name)
        if override is not None:
            return override
        return pick_rule(self.columns_by_name[column_name].native_type)


def _resolve_columns(connector: Connector, table: TableRef) -> dict[str, Column]:
    return {c.name: c for c in connector.get_schema(table)}


def _resolve_key_columns(
    connector: Connector, table: TableRef, key_columns: list[str] | None
) -> list[str]:
    if key_columns:
        return list(key_columns)
    pk = connector.get_primary_key(table)
    if not pk:
        raise NoPrimaryKeyError(table.qualified_name)
    return list(pk)


def _build_plan(
    connector: Connector,
    table: TableRef,
    key_columns: list[str] | None,
    columns: list[str] | None,
    exclude: list[str] | None,
    where: str | None = None,
) -> _Plan:
    cols_by_name = _resolve_columns(connector, table)
    resolved_key = _resolve_key_columns(connector, table, key_columns)

    if columns:
        selected = list(columns)
    else:
        selected = list(cols_by_name.keys())
    exclude_set = set(exclude or [])
    value_columns = [
        c for c in selected if c not in resolved_key and c not in exclude_set
    ]

    key_rule = pick_rule(cols_by_name[resolved_key[0]].native_type)

    return _Plan(
        connector=connector,
        table=table,
        columns=list(cols_by_name.values()),
        columns_by_name=cols_by_name,
        key_columns=resolved_key,
        value_columns=value_columns,
        key_rule=key_rule,
        where=where,
    )


def _quoted_table(connector: Connector, table: TableRef) -> str:
    if table.schema:
        return f"{connector.quote_identifier(table.schema)}.{connector.quote_identifier(table.table)}"
    return connector.quote_identifier(table.table)


def _with_where(sql: str, extra_where: str | None) -> str:
    """Append a `--where` filter (spec §7) to a query that has no WHERE
    clause of its own yet."""
    if not extra_where:
        return sql
    return f"{sql} WHERE ({extra_where})"


_BYTE_ORDER_SAFE_COLLATIONS = {"c", "posix", "c.utf8", "c.utf-8", "ucs_basic"}


def _random_order_expr(connector: Connector) -> str:
    """`ORDER BY <this>` for drawing a random sample (_sample_sql, _bisect).
    Postgres's `RANDOM()` is not standard SQL and not portable — ClickHouse
    has no function by that name at all (`SELECT ... ORDER BY RANDOM()`
    fails with "Function with name 'RANDOM' does not exist", confirmed
    against a real server; its own equivalent is `rand()`, lowercase,
    returning a plain UInt32). FakeConnector's sqlite backing understands
    `RANDOM()` natively (it's one of the few spellings sqlite and Postgres
    happen to share), so "fake" stays on the Postgres branch rather than
    needing a third case.
    """
    if connector.engine == "clickhouse":
        return "rand()"
    return "RANDOM()"


def _key_expr_for_ordering(
    connector: Connector, key_col: str, numeric: bool, native_type: str, collation: str | None
) -> str:
    """The one expression every bounds/sample/segment-filter query compares
    a non-numeric key against, so they all agree on a single total order —
    while comparing the RAW column whenever that's provably safe, since an
    indexed range scan is the whole point of segmentation and wrapping the
    column in an expression (a CAST, or a COLLATE that doesn't match the
    index's own collation) stops the planner from using the PK index at
    all (confirmed with EXPLAIN against a real Postgres).

    Left to a query's own default, a database's *default* collation is
    usually locale-aware (case- and sometimes punctuation-sensitive
    ordering), not a plain byte compare. If the sample used to pick
    quantile boundaries sorted one way while a segment's `WHERE lo <= key
    < hi` compared another way, a key could satisfy neither boundary
    (silently dropped from every segment) or both of two adjacent ones
    (double-counted) — exactly the kind of gap that lets a real difference
    vanish for mixed-case or non-ASCII text keys.

    Numeric keys never call this — arithmetic bounds (min/max/midpoint)
    are used for those, not a sampled sort order, so no such mismatch is
    possible there.

    Shapes, in order:

    * `uuid` — always the RAW column. uuid has its own native btree
      opclass and isn't a collatable type at all (`COLLATE` on a uuid
      expression is a Postgres error, not a no-op), so there is no
      collation ambiguity to resolve in the first place. (True for
      ClickHouse's UUID too — its comparison is always plain byte order,
      never locale-aware.)
    * `connector.engine == "clickhouse"` — always the RAW column. This
      function's `COLLATE "C"` forcing exists purely to work around
      Postgres's specific default-collation ambiguity (see below); spec's
      other v1 engine, ClickHouse, has no per-column locale-aware default
      collation concept at all — String/FixedString comparison is always
      plain byte order unless a query opts into `ORDER BY ... COLLATE`
      explicitly, which this project never does. Emitting Postgres's own
      `COLLATE "C"` SYNTAX against ClickHouse isn't just unnecessary,
      it's invalid SQL there (confirmed against a real server: `COLLATE`
      on an arbitrary expression is a syntax error, not a no-op — unlike
      Postgres, where it's at worst a redundant no-op). Deliberately an
      opt-in check for the one engine this has actually been verified
      against, not a catch-all `!= "postgres"` — `tests/unit/
      fake_connector.py`'s FakeConnector reports `engine = "fake"`
      specifically so its unit tests keep exercising this exact
      forced-`COLLATE "C"` SQL shape against SQLite (see its own
      docstring); widening this to "anything but Postgres" would silently
      stop testing that, for no engine actually confirmed to need it.
    * (Postgres only, from here) a `collation` this connector reports as
      already byte-order-safe (`_BYTE_ORDER_SAFE_COLLATIONS` — Postgres's
      own "C" and "POSIX", or the libc "C.UTF-8" locale this dev sandbox
      happens to default to) — also the RAW column. Forcing `COLLATE "C"`
      here would be correct but pointless: it's provably the same order
      the column (and its PK index) already use, so comparing the raw
      column keeps the index usable while losing nothing.
    * anything else (an unrecognised or locale-aware Postgres collation —
      e.g. a typical production `en_US.utf8` default (this project's own
      docker-compose.yml Postgres image included — confirmed directly),
      or the `en-x-icu` this project's own regression test uses — and a
      defensive fallback when a connector doesn't report collation at
      all, `collation is None`) — `col COLLATE "C"`, forced explicitly.
      This is the one case where a real, unavoidable trade-off exists:
      guaranteeing one deterministic order across bounds/sample/segment
      queries takes priority over the index, so this accepts a sequential
      scan rather than risk the silent-data-loss bug
      `clamp_to_range`/`assert_contiguous_coverage` exist to prevent.
      Confirmed both ways with EXPLAIN against real Postgres: a
      "C"/"C.UTF-8"-default column keeps its Index Scan; an `en_US.utf8`
      or `en-x-icu` one falls back to a Seq Scan.
    """
    q = connector.quote_identifier(key_col)
    if numeric:
        return q
    if native_type.strip().lower() == "uuid":
        return q
    if connector.engine == "clickhouse":
        return q
    if collation is not None and collation.strip().lower() in _BYTE_ORDER_SAFE_COLLATIONS:
        return q
    return f'{q} COLLATE "C"'


def _bounds_key_expr(
    connector: Connector, key_col: str, numeric: bool, native_type: str, collation: str | None
) -> str:
    """Like `_key_expr_for_ordering`, but specifically for the MIN/MAX
    bounds query, where uuid needs one further exception — on Postgres
    only: Postgres has no `MIN`/`MAX` *aggregate* registered for the uuid
    type at all (confirmed against a real instance — `SELECT
    MIN(uuid_col)` fails with "function min(uuid) does not exist"; this
    is despite uuid having full ordering operators, which is exactly why
    the raw column works fine as a *comparison* target in
    `_key_expr_for_ordering`'s segment predicates). So the bounds query
    alone still needs the CAST-to-TEXT workaround for uuid on Postgres —
    MIN/MAX(text) is a real aggregate.

    That cast's ordering must still agree with the raw-column ordering
    `_key_expr_for_ordering` uses everywhere else, or the "true min" this
    produces could disagree with what the segment predicates actually
    cover (the same class of bug clamp_to_range exists to prevent).
    Verified directly against Postgres: canonical lowercase uuid text is
    fixed-width with hyphens at fixed positions, so byte-order
    (`COLLATE "C"`) comparison of the text form exactly matches uuid's own
    native byte comparison — `ORDER BY v` and `ORDER BY v::text COLLATE
    "C"` produced identical orderings for a mixed-case sample. This is
    the one place that cast is added back; everywhere else uses the raw
    column so segment queries can still use the PK index.

    Bounds runs once per side, not once per segment, so this one text
    cast is not on the hot path segmentation cares about.

    ClickHouse needs none of this: it has a native `MIN`/`MAX(UUID)`
    aggregate (confirmed against a real server), so the raw column is
    both correct and sufficient there — and the Postgres workaround's
    exact SQL (`CAST(... AS TEXT) COLLATE "C"`) is a ClickHouse syntax
    error if applied anyway (same `COLLATE` issue as
    `_key_expr_for_ordering`).
    """
    if connector.engine == "postgres" and not numeric and native_type.strip().lower() == "uuid":
        q = connector.quote_identifier(key_col)
        return f'CAST({q} AS TEXT) COLLATE "C"'
    return _key_expr_for_ordering(connector, key_col, numeric, native_type, collation)


def _bounds_sql(
    connector: Connector,
    table: TableRef,
    key_col: str,
    numeric: bool,
    native_type: str,
    collation: str | None,
    extra_where: str | None = None,
) -> str:
    target = _bounds_key_expr(connector, key_col, numeric, native_type, collation)
    sql = f"SELECT MIN({target}), MAX({target}), COUNT(*) FROM {_quoted_table(connector, table)}"
    return _with_where(sql, extra_where)


def _sample_sql(
    connector: Connector,
    table: TableRef,
    key_col: str,
    sample_cap: int,
    native_type: str,
    collation: str | None,
    extra_where: str | None = None,
) -> str:
    # Always used for non-numeric keys only (numeric segmentation never
    # samples) — see segment_by_samples's caller.
    q = connector.quote_identifier(key_col)
    order_expr = _key_expr_for_ordering(
        connector, key_col, numeric=False, native_type=native_type, collation=collation
    )
    inner = f"SELECT {q} FROM {_quoted_table(connector, table)}"
    inner = _with_where(inner, extra_where)
    inner = f"{inner} ORDER BY {_random_order_expr(connector)} LIMIT {sample_cap}"
    # The random subset is drawn here (for a representative sample of the
    # distribution); it is then sorted server-side, under the exact same
    # ordering the segment WHERE clauses use, so the caller never needs to
    # sort in Python — Python's str comparison isn't guaranteed to agree
    # with SQL COLLATE "C" for every codepoint, and that's exactly the kind
    # of disagreement that lets a key fall between two segments unnoticed.
    return f"SELECT {q} FROM ({inner}) sampled ORDER BY {order_expr}"


def _duplicate_check_sql(
    connector: Connector, table: TableRef, key_columns: list[str], extra_where: str | None = None
) -> str:
    # Scoped by --where, not the whole table: a duplicate key that only
    # exists *outside* the filtered comparison scope shouldn't block a diff
    # that never intends to look at it (e.g. duplicates among rows a
    # `--where status = 'active'` filter excludes).
    cols = ", ".join(connector.quote_identifier(c) for c in key_columns)
    sql = f"SELECT {cols}, COUNT(*) FROM {_quoted_table(connector, table)}"
    sql = _with_where(sql, extra_where)
    return f"{sql} GROUP BY {cols} HAVING COUNT(*) > 1 LIMIT 1"


def _key_column(plan: _Plan) -> Column:
    return plan.columns_by_name[plan.key_columns[0]]


def _segment_where(
    connector: Connector,
    key_columns: list[str],
    segment: Segment,
    numeric: bool,
    native_type: str,
    collation: str | None,
) -> str:
    """WHERE clause bounding the *first* key column to this segment's
    [lo, hi) range. (Composite keys are range-bounded on their leading
    column only — later columns are still fully covered because they ride
    along inside each row; this keeps segmentation simple while still
    correctly answering "do the tables match" for composite keys.)

    Compares the same expression `_key_expr_for_ordering` builds for
    bounds/sampling — never a different one — so a key's segment
    membership here always agrees with the ordering that produced the
    segment boundaries in the first place.
    """
    col = _key_expr_for_ordering(connector, key_columns[0], numeric, native_type, collation)
    lo_lit = connector.quote_literal(segment.lo)
    if segment.hi is None:
        return f"{col} >= {lo_lit}"
    hi_lit = connector.quote_literal(segment.hi)
    return f"{col} >= {lo_lit} AND {col} < {hi_lit}"


def _combined_where(plan: _Plan, segment: Segment) -> str:
    """The segment's own range bound, AND'd with the plan's --where filter
    (spec §7) when one was given. Every per-segment query goes through
    this — plan.where is read directly off the plan rather than threaded
    as a parameter through every call site, so it can't be forgotten."""
    numeric = plan.key_rule in _NUMERIC_RULES
    key_col = _key_column(plan)
    where = _segment_where(
        plan.connector, plan.key_columns, segment, numeric, key_col.native_type, key_col.collation
    )
    if plan.where:
        where = f"({where}) AND ({plan.where})"
    return where


def _segment_stats_sql(plan: _Plan, segment: Segment) -> str:
    connector = plan.connector
    hash_columns = [*plan.key_columns, *plan.value_columns]
    exprs = [
        connector.normalise_expr(plan.columns_by_name[c], plan.rule_for(c), plan.options)
        for c in hash_columns
    ]
    row_hash = connector.row_hash_expr(exprs)
    agg_hash = connector.aggregate_hash_expr(row_hash)
    where = _combined_where(plan, segment)
    return (
        f"SELECT COUNT(*), {agg_hash} FROM {_quoted_table(connector, plan.table)} "
        f"WHERE {where}"
    )


def _row_fetch_sql(plan: _Plan, segment: Segment) -> str:
    connector = plan.connector
    # Keys are selected in their *native* form, not normalised: M0 only
    # diffs one engine against itself, so there's no cross-engine type
    # mismatch to paper over yet (that's spec §6.3, needed from M2 on), and
    # keeping keys native means RowDiff.key carries real ints/uuids rather
    # than every key becoming a string. Value columns still go through
    # normalise_expr since *they* are what gets compared for equality.
    key_exprs = [connector.quote_identifier(c) for c in plan.key_columns]
    value_exprs = [
        connector.normalise_expr(plan.columns_by_name[c], plan.rule_for(c), plan.options)
        for c in plan.value_columns
    ]
    select_list = ", ".join([*key_exprs, *value_exprs])
    order_by = ", ".join(connector.quote_identifier(c) for c in plan.key_columns)
    where = _combined_where(plan, segment)
    return (
        f"SELECT {select_list} FROM {_quoted_table(connector, plan.table)} "
        f"WHERE {where} ORDER BY {order_by}"
    )


def _validate_key_unique_side(plan: _Plan) -> None:
    # Always verify uniqueness — cheap relative to the diff itself, and a
    # silently non-unique key produces meaningless results (spec §4.4).
    # Scoped by plan.where: uniqueness is checked within the comparison's
    # own scope, not the whole table (see _duplicate_check_sql's docstring).
    sql = _duplicate_check_sql(plan.connector, plan.table, plan.key_columns, plan.where)
    rows = plan.connector.query(sql)
    if rows:
        dup = rows[0][:-1]  # drop the COUNT(*) column
        raise NonUniqueKeyError(plan.table.qualified_name, plan.key_columns, tuple(dup))


def _get_bounds(plan: _Plan) -> tuple:
    numeric = plan.key_rule in _NUMERIC_RULES
    key_col = _key_column(plan)
    sql = _bounds_sql(
        plan.connector, plan.table, plan.key_columns[0], numeric,
        key_col.native_type, key_col.collation, plan.where,
    )
    rows = plan.connector.query(sql)
    lo, hi, count = rows[0]
    return lo, hi, (count or 0)


def _initial_segments(plan: _Plan, count: int, lo, hi) -> list[Segment]:
    num_segments = compute_num_segments(count)
    if count == 0:
        return [Segment(index=0, lo=lo, hi=None)]
    if plan.key_rule in _NUMERIC_RULES:
        segments = segment_numeric_range(lo, hi, num_segments)
    else:
        key_col = _key_column(plan)
        sample_sql = _sample_sql(
            plan.connector, plan.table, plan.key_columns[0], DEFAULT_SAMPLE_CAP,
            key_col.native_type, key_col.collation, plan.where,
        )
        # Already sorted server-side (see _sample_sql) — never re-sort in
        # Python, which could silently disagree with the DB's COLLATE "C".
        samples = [r[0] for r in plan.connector.query(sample_sql)]
        segments = segment_by_samples(samples, num_segments)

    # `lo`/`hi` here are the TRUE min/max across both sides (from the
    # bounds query, computed by the caller) — not whatever a *sample*
    # happened to catch. Force the first segment down to the real lo (see
    # clamp_to_range's docstring for why this matters) and keep the
    # top-level last segment open-ended (hi=None) so a max-side insert
    # since the bounds query ran is still covered.
    segments = clamp_to_range(segments, lo, None)
    assert_contiguous_coverage(segments, lo, None)
    return segments


def _bisect(plan: _Plan, segment: Segment, lo, hi) -> list[Segment]:
    """Split one segment's [lo, hi) range in half, for recursive bisection.

    `open_ended_last=False` is essential here (see segmentation.py's
    docstring): this is bisecting an already-*bounded* segment, so the
    second half's hi must stay at the segment's real, bounded upper edge —
    never fall back to "open, catches everything above" or it would
    double-count rows that belong to a sibling segment.
    """
    if hi is None:
        # The outermost, unbounded top-level segment: no arithmetic
        # midpoint exists, so fall back straight to a full row-level fetch
        # of it instead (still correct, just not maximally fast).
        return [segment]
    if plan.key_rule in _NUMERIC_RULES:
        # max_val is inclusive in segment_numeric_range's contract; our hi
        # is exclusive, so pass hi - 1 as max_val.
        segments = segment_numeric_range(lo, hi - 1, 2, open_ended_last=False)
        segments = clamp_to_range(segments, lo, hi)
        assert_contiguous_coverage(segments, lo, hi)
        return segments
    # For non-numeric keys we don't have an arithmetic midpoint; re-sample
    # within this segment's range instead (also scoped by plan.where, same
    # as every other query here — the sample must reflect only the rows
    # actually in scope for the comparison). Same two-step shape as
    # _sample_sql: draw a random subset, then let the DB sort it under
    # COLLATE "C" — never in Python (see _sample_sql's comment for why).
    where = _combined_where(plan, segment)
    key_column = _key_column(plan)
    key_col = plan.connector.quote_identifier(plan.key_columns[0])
    order_expr = _key_expr_for_ordering(
        plan.connector, plan.key_columns[0], numeric=False,
        native_type=key_column.native_type, collation=key_column.collation,
    )
    inner_sql = (
        f"SELECT {key_col} FROM {_quoted_table(plan.connector, plan.table)} "
        f"WHERE {where} ORDER BY {_random_order_expr(plan.connector)} LIMIT {DEFAULT_SAMPLE_CAP}"
    )
    sample_sql = f"SELECT {key_col} FROM ({inner_sql}) sampled ORDER BY {order_expr}"
    samples = [r[0] for r in plan.connector.query(sample_sql)]
    if not samples:
        # Nothing on this side to split on — bisection can't help; the
        # caller falls back to a direct row-level fetch of the whole
        # (still exact) [lo, hi) segment.
        return [segment]
    sub_segments = segment_by_samples(samples, 2)
    # segment_by_samples's boundaries come from a sample of *this already-
    # bounded* segment, so its own lo/hi can drift from the segment's real,
    # exact [lo, hi) the same way a top-level sample can drift from the
    # table's true min — clamp both ends back to the parent's exact
    # boundaries (never leaving the last bucket open here: it would spill
    # into a sibling segment's range) and verify the tiling is exact.
    sub_segments = clamp_to_range(sub_segments, lo, hi)
    assert_contiguous_coverage(sub_segments, lo, hi)
    return sub_segments


def _apply_column_match(
    src_plan: _Plan, tgt_plan: _Plan, result: DiffResult, options: NormaliseOptions, column_map: dict | None
) -> None:
    """Run spec §6.2 column matching once both sides' schemas are known,
    and apply its result to both plans so they render matched columns
    identically (same rule, same DEC-1 scale / TS-2 precision).

    `matched` names are always the *source's* names — including for a
    `--column-map`-renamed pair, where the target's column is genuinely
    called something else. To keep every downstream lookup
    (`plan.columns_by_name[c]`, `plan.rule_for(c)`, `plan.options.
    scale_overrides[c]`) working off that ONE shared name on both plans
    without duplicating the whole matching dict by side, alias the
    target's `columns_by_name` so the canonical (source) name also
    resolves there — to the target's real Column object, whose own
    `.name` field is still its real name, so `normalise_expr` still
    quotes and queries the actual target column.
    """
    match = match_columns(
        src_plan.columns_by_name, tgt_plan.columns_by_name, src_plan.value_columns,
        column_map=column_map, base_options=options,
    )
    column_map = column_map or {}
    tgt_by_lower = {name.lower(): name for name in tgt_plan.columns_by_name}
    for name in match.matched:
        real_tgt_name = column_map.get(name) or tgt_by_lower.get(name.lower(), name)
        if real_tgt_name != name:
            tgt_plan.columns_by_name[name] = tgt_plan.columns_by_name[real_tgt_name]

    src_plan.value_columns = list(match.matched)
    tgt_plan.value_columns = list(match.matched)
    src_plan.rule_overrides = match.rule_overrides
    tgt_plan.rule_overrides = match.rule_overrides
    src_plan.options = match.options
    tgt_plan.options = match.options
    result.excluded_columns.extend(match.excluded)
    result.warnings.extend(match.warnings)
    _append_run_level_warnings(src_plan, result)


def _append_run_level_warnings(plan: _Plan, result: DiffResult) -> None:
    """TS-3 ("warn once per run") and UNK-1 ("warn once per column") from
    spec §6.1 — scanned once, off the already-matched plan, so a column
    upgraded to TS-2 (which supersedes the naive-timestamp warning with
    its own more specific one) isn't double-reported."""
    naive_cols = []
    for c in [*plan.key_columns, *plan.value_columns]:
        rule = plan.rule_for(c)
        if rule is NormalisationRule.TS_3:
            naive_cols.append(c)
        elif rule is NormalisationRule.UNK_1:
            native = plan.columns_by_name[c].native_type
            result.warnings.append(
                TdWarning(
                    f"UNK-1: column {c!r} has an unmapped type ({native!r}) — "
                    "compared as raw text",
                    rule=NormalisationRule.UNK_1,
                    column=c,
                )
            )
    if naive_cols:
        result.warnings.append(
            TdWarning(
                "TS-3: naive timestamp column(s) assumed "
                f"{plan.options.assume_tz} ({', '.join(naive_cols)}) — use --assume-tz to override",
                rule=NormalisationRule.TS_3,
            )
        )


def diff(
    source: Connector,
    target: Connector,
    source_table: TableRef,
    target_table: TableRef,
    key_columns: list[str] | None = None,
    columns: list[str] | None = None,
    exclude: list[str] | None = None,
    row_threshold: int = DEFAULT_ROW_THRESHOLD,
    max_diff_rows: int = DEFAULT_MAX_DIFF_ROWS,
    where: str | None = None,
    where_source: str | None = None,
    where_target: str | None = None,
    trim: bool = False,
    case_insensitive: bool = False,
    float_precision: int = 15,
    assume_tz: str = "UTC",
    column_map: dict | None = None,
    threads: int = 1,
    source_pool: list[Connector] | None = None,
    target_pool: list[Connector] | None = None,
) -> DiffResult:
    """spec §7: `--threads N` — "parallel segment queries per side
    (default 4)". `source`/`target` still do every single-shot query
    (schema, bounds, uniqueness) exactly as before; `threads > 1` only
    changes how the many independent per-*segment* queries run.

    Genuine parallelism needs genuinely separate connections — a single
    synchronous psycopg/clickhouse-connect connection can't run two
    queries at once — so the caller supplies `source_pool`/`target_pool`:
    `threads` extra, already-`connect()`-ed Connector instances per side
    (same engine, same table, just independent connections). Without
    both pools (the default), this always takes the exact sequential
    path — zero behavioural change for every existing caller. This
    doesn't relax spec §5's "One connection per side. No connection
    pooling in v1." rule so much as make it precise: a *pool* here means
    exactly `threads` fixed, caller-owned connections (never grown,
    never a general-purpose connection-pooling library), matching
    `--threads N` one-for-one — never more connections than the CLI flag
    the user asked for.
    """
    start_time = time.monotonic()
    # spec §7: --where applies to both sides; --where-source/--where-target
    # override it per side when given.
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
        algorithm=Algorithm.HASHDIFF,
    )
    _apply_column_match(src_plan, tgt_plan, result, options, column_map)

    _validate_key_unique_side(src_plan)
    _validate_key_unique_side(tgt_plan)

    src_lo, src_hi, src_count = _get_bounds(src_plan)
    tgt_lo, tgt_hi, tgt_count = _get_bounds(tgt_plan)
    result.source_count = src_count
    result.target_count = tgt_count
    if src_count != tgt_count:
        result.warnings.append(
            TdWarning(f"row counts differ: source={src_count} target={tgt_count}")
        )

    # Segment the union of both sides' ranges so a row only present on one
    # side (outside the other side's min/max) still gets examined.
    if src_count == 0 and tgt_count == 0:
        result.elapsed_seconds = time.monotonic() - start_time
        return result

    candidates = [v for v in (src_lo, tgt_lo) if v is not None]
    lo = min(candidates) if candidates else src_lo
    candidates = [v for v in (src_hi, tgt_hi) if v is not None]
    hi = max(candidates) if candidates else src_hi
    count_hint = max(src_count, tgt_count)

    segments = _initial_segments(src_plan, count_hint, lo, hi)
    result.segments_examined = len(segments)

    if threads > 1 and source_pool and target_pool:
        queries = _diff_segments_parallel(
            src_plan, tgt_plan, segments, row_threshold, max_diff_rows, result,
            min(threads, len(source_pool), len(target_pool)), source_pool, target_pool,
        )
    else:
        queries = _diff_segments_sequential(
            src_plan, tgt_plan, segments, row_threshold, max_diff_rows, result, source, target,
        )

    result.queries_per_side = queries // 2
    result.elapsed_seconds = time.monotonic() - start_time
    return result


def _diff_segments_sequential(
    src_plan: _Plan,
    tgt_plan: _Plan,
    segments: list[Segment],
    row_threshold: int,
    max_diff_rows: int,
    result: DiffResult,
    source: Connector,
    target: Connector,
) -> int:
    """One connection per side, one segment at a time — the default path
    (spec §5's plain "one connection per side"), unchanged from before
    `--threads` existed."""
    queries = 0
    for segment in segments:
        queries += 2  # one stats query per side
        src_sql = _segment_stats_sql(src_plan, segment)
        tgt_sql = _segment_stats_sql(tgt_plan, segment)
        src_stats = source.query(src_sql)[0]
        tgt_stats = target.query(tgt_sql)[0]
        src_seg_count, src_hash = src_stats
        tgt_seg_count, tgt_hash = tgt_stats

        if src_seg_count == tgt_seg_count and src_hash == tgt_hash:
            continue

        more_queries = _resolve_mismatched_segment(
            src_plan, tgt_plan, segment, src_seg_count, tgt_seg_count,
            row_threshold, max_diff_rows, result,
        )
        queries += more_queries
        # NOTE: we deliberately keep examining every segment even after
        # max_diff_rows is hit. Spec §4.1 step 6: "Cap collecting
        # row-level differences ... Counts stay exact; row lists don't."
        # Only the detailed RowDiff list stops growing (_maybe_append);
        # missing/extra/changed counters keep incrementing for every
        # segment so the summary numbers are always trustworthy even when
        # the row list is truncated.
    return queries


class _ConnectorPool:
    """A fixed, caller-owned set of already-`connect()`-ed connectors for
    one side — `threads` of them, never more, never grown (see `diff()`'s
    own docstring on why this isn't the "connection pooling" spec §5
    rules out). `acquire()`/`release()` hand one out and back via a
    `queue.Queue`, which is itself thread-safe, so many worker threads can
    share one pool without any extra locking here.
    """

    def __init__(self, connectors: list[Connector]) -> None:
        self._q: queue.Queue = queue.Queue()
        for c in connectors:
            self._q.put(c)

    def acquire(self) -> Connector:
        return self._q.get()

    def release(self, connector: Connector) -> None:
        self._q.put(connector)


def _plan_with_connector(plan: _Plan, connector: Connector) -> _Plan:
    """A shallow copy of `plan` pointed at a different connector — used
    only by the parallel segment path so each worker thread executes its
    queries over its own checked-out connection while sharing the plan's
    already-computed schema/column-matching state. Safe because every
    connector in a side's pool is logically identical (same engine, same
    table, same schema) — only the physical connection differs, and
    `plan.connector` is only ever used for two things, both invariant
    across a side's pool: generating SQL text (quote_identifier,
    normalise_expr, ...) and running `.query()`.
    """
    return dataclasses.replace(plan, connector=connector)


def _diff_segments_parallel(
    src_plan: _Plan,
    tgt_plan: _Plan,
    segments: list[Segment],
    row_threshold: int,
    max_diff_rows: int,
    result: DiffResult,
    threads: int,
    source_pool: list[Connector],
    target_pool: list[Connector],
) -> int:
    """Runs every top-level segment's stats check — and, for a mismatched
    one, that segment's *entire* recursive bisection — as one task on a
    `threads`-worker pool. Different segments run genuinely concurrently
    (each task checks out its own pair of connections for its whole
    lifetime, so there's no per-query pool churn); one segment's own
    bisection stays sequential within its task — the fan-out across many
    independent segments is where the real win is, and it avoids the
    added complexity of also parallelising a single segment's recursion.

    `result` is shared and mutated by every task (`_classify_rows`'s
    counters and `row_diffs` list) — `_lock` guards exactly that mutation
    (see `_resolve_mismatched_segment`'s own `lock` parameter), never the
    query round-trips themselves, so lock contention stays minimal.
    """
    src_conn_pool = _ConnectorPool(source_pool)
    tgt_conn_pool = _ConnectorPool(target_pool)
    lock = threading.Lock()
    query_count = 0
    query_count_lock = threading.Lock()

    def process_segment(segment: Segment) -> None:
        nonlocal query_count
        src_conn = src_conn_pool.acquire()
        tgt_conn = tgt_conn_pool.acquire()
        try:
            thread_src_plan = _plan_with_connector(src_plan, src_conn)
            thread_tgt_plan = _plan_with_connector(tgt_plan, tgt_conn)
            src_sql = _segment_stats_sql(thread_src_plan, segment)
            tgt_sql = _segment_stats_sql(thread_tgt_plan, segment)
            src_seg_count, src_hash = src_conn.query(src_sql)[0]
            tgt_seg_count, tgt_hash = tgt_conn.query(tgt_sql)[0]
            segment_queries = 2

            if not (src_seg_count == tgt_seg_count and src_hash == tgt_hash):
                segment_queries += _resolve_mismatched_segment(
                    thread_src_plan, thread_tgt_plan, segment, src_seg_count, tgt_seg_count,
                    row_threshold, max_diff_rows, result, lock=lock,
                )
        finally:
            src_conn_pool.release(src_conn)
            tgt_conn_pool.release(tgt_conn)

        with query_count_lock:
            query_count += segment_queries

    with ThreadPoolExecutor(max_workers=threads) as executor:
        futures = [executor.submit(process_segment, segment) for segment in segments]
        for future in as_completed(futures):
            future.result()  # re-raise anything a worker thread raised

    return query_count


def _resolve_mismatched_segment(
    src_plan: _Plan,
    tgt_plan: _Plan,
    segment: Segment,
    src_seg_count: int,
    tgt_seg_count: int,
    row_threshold: int,
    max_diff_rows: int,
    result: DiffResult,
    depth: int = 0,
    lock: threading.Lock | None = None,
) -> int:
    """`lock`, when given (only from `_diff_segments_parallel`), guards
    every mutation of the shared `result` object — never the query
    round-trips, which is where the real time goes and where holding a
    lock would just serialise the parallel path back into a sequential
    one. `None` (the default, used by the sequential path) means "no
    other thread can be touching `result`, skip locking entirely" — the
    exact previous behaviour, unconditionally.
    """
    biggest = max(src_seg_count, tgt_seg_count)
    queries = 0

    if biggest > row_threshold and depth < 20:
        # Bisect: recompute stats on each half instead of fetching rows.
        sub_segments = _bisect(src_plan, segment, segment.lo, segment.hi)
        if len(sub_segments) > 1:
            any_real_split = False
            for sub in sub_segments:
                queries += 2
                s_sql = _segment_stats_sql(src_plan, sub)
                t_sql = _segment_stats_sql(tgt_plan, sub)
                s_stats = src_plan.connector.query(s_sql)[0]
                t_stats = tgt_plan.connector.query(t_sql)[0]
                if s_stats == t_stats:
                    continue
                any_real_split = True
                queries += _resolve_mismatched_segment(
                    src_plan, tgt_plan, sub, s_stats[0], t_stats[0],
                    row_threshold, max_diff_rows, result, depth + 1, lock,
                )
            if any_real_split:
                return queries
            # Every sub-segment came back looking equal even though the
            # parent didn't. For an order-independent additive hash that
            # should be mathematically impossible — the sub-segments'
            # stats sum to the parent's — so this means the segmentation
            # itself has a coverage bug we haven't caught (e.g. a gap that
            # let the same excluded rows through the counts on both sides
            # equally). Never trust that contradiction silently: fall
            # through to a direct row-level fetch of the whole parent
            # segment instead, so the real difference still gets found
            # and reported rather than vanishing.

    # Small enough (or bisection didn't explain the mismatch): fetch full
    # rows from both sides and classify client-side.
    queries += 2
    src_sql = _row_fetch_sql(src_plan, segment)
    tgt_sql = _row_fetch_sql(tgt_plan, segment)
    src_rows = src_plan.connector.query(src_sql)
    tgt_rows = tgt_plan.connector.query(tgt_sql)

    _classify_rows(src_plan, tgt_plan, src_rows, tgt_rows, max_diff_rows, result, lock)
    return queries


def _classify_rows(src_plan, tgt_plan, src_rows, tgt_rows, max_diff_rows, result: DiffResult, lock=None) -> None:
    n_key = len(src_plan.key_columns)
    value_names = src_plan.value_columns

    def key_of(row):
        return tuple(row[:n_key])

    src_by_key = {key_of(r): r for r in src_rows}
    tgt_by_key = {key_of(r): r for r in tgt_rows}
    all_keys = sorted(set(src_by_key) | set(tgt_by_key))

    def _classify() -> None:
        for key in all_keys:
            s = src_by_key.get(key)
            t = tgt_by_key.get(key)
            if s is not None and t is None:
                result.missing_in_target += 1
                _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="missing"))
            elif s is None and t is not None:
                result.extra_in_target += 1
                _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="extra"))
            else:
                changes = {}
                for i, col in enumerate(value_names):
                    sv = s[n_key + i]
                    tv = t[n_key + i]
                    if sv != tv:
                        rule = src_plan.rule_for(col)
                        changes[col] = (sv, tv, rule)
                if changes:
                    result.changed += 1
                    _maybe_append(result, max_diff_rows, RowDiff(key=key, kind="changed", changes=changes))
            # Deliberately no early return here: every key keeps getting
            # classified and counted even once the row-detail cap is hit
            # (see the note in diff()) — only _maybe_append stops growing
            # the list.

    # The classification itself is pure in-memory work (no I/O) — cheap
    # enough that holding `lock` for the whole thing, rather than
    # per-counter, costs nothing measurable and is far simpler to reason
    # about than fine-grained locking would be.
    if lock is not None:
        with lock:
            _classify()
    else:
        _classify()


def _maybe_append(result: DiffResult, max_diff_rows: int, row_diff: RowDiff) -> None:
    if len(result.row_diffs) >= max_diff_rows:
        result.truncated = True
        return
    result.row_diffs.append(row_diff)


def explain(
    source: Connector,
    target: Connector,
    source_table: TableRef,
    target_table: TableRef,
    key_columns: list[str] | None = None,
    columns: list[str] | None = None,
    exclude: list[str] | None = None,
    where: str | None = None,
    where_source: str | None = None,
    where_target: str | None = None,
    trim: bool = False,
    case_insensitive: bool = False,
    float_precision: int = 15,
    assume_tz: str = "UTC",
    column_map: dict | None = None,
) -> list[str]:
    """Return the SQL tablediff would run, without executing any of it.

    Segment boundaries can't be known without running the bounds query
    (which explain must not do), so the per-segment statement is shown as
    a template against segment 0's shape with placeholder bounds.
    """
    src_where = where_source or where
    tgt_where = where_target or where
    src_plan = _build_plan(source, source_table, key_columns, columns, exclude, src_where)
    tgt_plan = _build_plan(target, target_table, key_columns, columns, exclude, tgt_where)
    options = NormaliseOptions(
        trim=trim, case_insensitive=case_insensitive,
        float_precision=float_precision, assume_tz=assume_tz,
    )
    _apply_column_match(src_plan, tgt_plan, DiffResult(
        source=source_table, target=target_table,
        key_columns=tuple(src_plan.key_columns), algorithm=Algorithm.HASHDIFF,
    ), options, column_map)

    statements = [
        "-- source: row-count and bounds",
        _bounds_sql(
            source, source_table, src_plan.key_columns[0], src_plan.key_rule in _NUMERIC_RULES,
            _key_column(src_plan).native_type, _key_column(src_plan).collation, src_plan.where,
        ),
        "-- target: row-count and bounds",
        _bounds_sql(
            target, target_table, tgt_plan.key_columns[0], tgt_plan.key_rule in _NUMERIC_RULES,
            _key_column(tgt_plan).native_type, _key_column(tgt_plan).collation, tgt_plan.where,
        ),
        "-- source: uniqueness check on the key",
        _duplicate_check_sql(source, source_table, src_plan.key_columns, src_plan.where),
        "-- target: uniqueness check on the key",
        _duplicate_check_sql(target, target_table, tgt_plan.key_columns, tgt_plan.where),
    ]

    placeholder_segment = Segment(index=0, lo="<segment lower bound>", hi="<segment upper bound>")
    statements.append("-- source: per-segment count + hash (one per segment)")
    statements.append(_segment_stats_sql(src_plan, placeholder_segment))
    statements.append("-- target: per-segment count + hash (one per segment)")
    statements.append(_segment_stats_sql(tgt_plan, placeholder_segment))
    statements.append("-- source: row-level fetch for a mismatched segment")
    statements.append(_row_fetch_sql(src_plan, placeholder_segment))
    statements.append("-- target: row-level fetch for a mismatched segment")
    statements.append(_row_fetch_sql(tgt_plan, placeholder_segment))

    return statements
