"""Pure-Python data model for the diff algorithm.

Zero database imports live here (spec §0 rule 4) — everything in this
module is plain dataclasses/enums so it can be exercised in unit tests with
fake connectors.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


@dataclass(frozen=True)
class TableRef:
    """Identifies one table on one connection."""

    engine: str
    database: str
    table: str
    schema: str | None = None

    @property
    def qualified_name(self) -> str:
        if self.schema:
            return f"{self.schema}.{self.table}"
        return self.table

    def __str__(self) -> str:  # pragma: no cover - convenience only
        return f"{self.engine}:{self.database}/{self.qualified_name}"


@dataclass(frozen=True)
class Column:
    """One column, as reported by a connector's get_schema()."""

    name: str
    native_type: str
    nullable: bool
    ordinal: int
    # The column's actual declared/default collation, when the connector
    # can report one (None for non-collatable types, or when a connector
    # doesn't support this yet — FakeConnector leaves it unset). Used only
    # as a perf hint for non-numeric-key segmentation (core/hashdiff.py):
    # whether a segment's range predicate can compare the column's own
    # raw value (letting its PK index be used) or must force a specific
    # collation for correctness (at the cost of an index scan). Never
    # affects correctness on its own — the safe default when unknown is
    # to force a fixed collation.
    collation: str | None = None
    # DEC-1 (§6.1): decimal places for a fixed-point numeric column, when
    # the connector can report one (None for non-decimal types). Needed to
    # compute the *pair's* min scale across both sides at schema-match
    # time (spec §6.2) — a single column's own scale isn't enough on its
    # own, since the canonical rendering must agree with whatever the
    # other side declares.
    scale: int | None = None
    # TS-2 (§6.1): fractional-second precision for a timestamp/time
    # column, when the connector can report one (None otherwise). Same
    # cross-side min-precision purpose as `scale`, for timestamp instead
    # of decimal columns.
    precision: int | None = None


@dataclass(frozen=True)
class NormaliseOptions:
    """Global rendering knobs from CLI flags (spec §7), plus per-column
    overrides computed once at schema-match time (spec §6.2) from
    comparing *both* sides' declared scale/precision — a single column's
    own metadata never determines these alone, since the canonical form
    must agree with whatever the other side declares. Passed to
    `Connector.normalise_expr()`; a connector that ignores an option
    (e.g. FakeConnector) just renders the untruncated/untrimmed form.
    """

    trim: bool = False  # STR-2: --trim strips trailing whitespace
    case_insensitive: bool = False  # STR-2: --case-insensitive lower-cases
    float_precision: int = 15  # FLT-1: significant digits, --float-precision
    assume_tz: str = "UTC"  # TS-3: naive-timestamp zone, --assume-tz
    scale_overrides: dict[str, int] = field(default_factory=dict)  # DEC-1: column -> min scale
    precision_overrides: dict[str, int] = field(default_factory=dict)  # TS-2: column -> min precision


class NormalisationRule(str, Enum):
    """Rule IDs from spec §6.1. M0 implements only the four marked M0."""

    NULL_1 = "NULL-1"  # M0
    INT_1 = "INT-1"  # M0
    DEC_1 = "DEC-1"
    FLT_1 = "FLT-1"
    STR_1 = "STR-1"  # M0
    STR_2 = "STR-2"
    BOOL_1 = "BOOL-1"
    TS_1 = "TS-1"  # M0
    TS_2 = "TS-2"
    TS_3 = "TS-3"
    DATE_1 = "DATE-1"
    TIME_1 = "TIME-1"
    UUID_1 = "UUID-1"
    BIN_1 = "BIN-1"
    JSON_1 = "JSON-1"
    ARR_1 = "ARR-1"
    ENUM_1 = "ENUM-1"
    UNK_1 = "UNK-1"


class Algorithm(str, Enum):
    HASHDIFF = "hashdiff"
    JOINDIFF = "joindiff"
    AUTO = "auto"


@dataclass(frozen=True)
class Segment:
    """A key range on both sides of a diff: [lo, hi) in canonical-sortable form."""

    index: int
    lo: Any
    hi: Any | None  # None means "open ended" (the final segment)


@dataclass
class SegmentResult:
    segment: Segment
    row_count: int
    row_hash: Any


@dataclass
class RowDiff:
    """One row-level difference, already classified."""

    key: tuple
    kind: str  # "missing" | "extra" | "changed"
    changes: dict[str, tuple[Any, Any, NormalisationRule | None]] = field(
        default_factory=dict
    )
    # changes: column -> (source_value, target_value, rule_that_applied)


@dataclass
class Warning:
    message: str
    rule: NormalisationRule | None = None
    column: str | None = None


@dataclass
class DiffResult:
    source: TableRef
    target: TableRef
    key_columns: tuple[str, ...]
    algorithm: Algorithm
    source_count: int = 0
    target_count: int = 0
    row_diffs: list[RowDiff] = field(default_factory=list)
    truncated: bool = False
    missing_in_target: int = 0
    extra_in_target: int = 0
    changed: int = 0
    warnings: list[Warning] = field(default_factory=list)
    excluded_columns: list[str] = field(default_factory=list)
    segments_examined: int = 0
    queries_per_side: int = 0
    # spec §8.1's terminal example shows a trailing "18.4s" on the
    # algorithm line, and §8.2's JSON output requires "timings" — wall
    # clock time for the whole diff()/explain() call, set by the caller.
    elapsed_seconds: float = 0.0
    # spec §4.3: "--sample 1% ... Report results as 'of the sampled rows'
    # with the sample size stated. Never silently sample." None means no
    # sampling was requested — every count above is the real, whole-table
    # figure. A number (0-100) means `--sample`/`--sample-rows` was used;
    # every count above already reflects only the sampled subset (the
    # sampling predicate is folded into the same WHERE every bounds/
    # segment/count query already uses, spec §2's "never pull full tables
    # to the client" applying just as much to a sampled run), and the
    # renderer's job is to make that fact impossible to miss, not to
    # recompute anything.
    sample_pct: float | None = None

    @property
    def fully_compared(self) -> bool:
        """False when any column was left out of the comparison (a type
        mismatch, or a column that exists on only one side). A MATCH in
        that case only covers the columns that WERE compared."""
        return not self.excluded_columns

    @property
    def is_match(self) -> bool:
        return (
            self.source_count == self.target_count
            and self.missing_in_target == 0
            and self.extra_in_target == 0
            and self.changed == 0
        )

    def exit_code(self) -> int:
        return 0 if self.is_match else 1
