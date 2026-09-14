"""Splitting a key range into segments for hashdiff (spec §4.1 step 2).

Two independent strategies, both pure functions of values the caller
already has in hand (a connector's MIN/MAX/COUNT, or a sampled list of key
values) — no I/O happens in this module.
"""

from __future__ import annotations

import math

from tablediff.core.models import Segment

DEFAULT_TARGET_ROWS_PER_SEGMENT = 100_000
DEFAULT_MIN_SEGMENTS = 8


def compute_num_segments(
    count: int,
    target_rows_per_segment: int = DEFAULT_TARGET_ROWS_PER_SEGMENT,
    min_segments: int = DEFAULT_MIN_SEGMENTS,
) -> int:
    """How many segments to split [min, max] into.

    An empty table is one trivial segment. Otherwise: enough segments to
    keep each one near `target_rows_per_segment`, rounded up so no segment
    silently absorbs more than the target, but never fewer than
    `min_segments` — small tables still get bisected usefully.
    """
    if count <= 0:
        return 1
    by_target = math.ceil(count / target_rows_per_segment)
    return max(min_segments, by_target)


def segment_numeric_range(
    min_val, max_val, num_segments: int, open_ended_last: bool = True
) -> list[Segment]:
    """Cut [min_val, max_val] into `num_segments` equal-width slices.

    Works for any orderable numeric key (int, decimal). Boundaries are
    computed in Python and handed back to the connector, which translates
    them into native literals for its own WHERE clause (spec §6.3).

    By default (`open_ended_last=True`) the final segment's `hi` is None
    (open-ended, `>= lo`) so the true maximum — and anything inserted above
    it since the bounds query ran — is still covered. This is right for the
    *top-level* segmentation of a whole table, but wrong when bisecting an
    already-bounded segment during recursion (spec §4.1 step 4): the parent
    segment's own `hi` is a hard boundary, not the table's true max, so
    letting the last half "spill" past it would double-count rows that
    belong to the *next* sibling segment. Pass `open_ended_last=False` for
    that case — the final slice's `hi` is then the real, bounded
    `max_val + 1` instead of None.
    """
    if min_val == max_val:
        return [Segment(index=0, lo=min_val, hi=None if open_ended_last else max_val + 1)]

    span = max_val - min_val
    # Width is computed over span + 1 so that for dense integer keys
    # (1..N) every unit value maps into exactly one segment and the
    # segments come out balanced (see the sequential-PK balance test).
    width = (span + 1) / num_segments

    segments = []
    for i in range(num_segments):
        lo = min_val + round(i * width)
        if i == num_segments - 1:
            hi = None if open_ended_last else min_val + round((i + 1) * width)
        else:
            hi = min_val + round((i + 1) * width)
        segments.append(Segment(index=i, lo=lo, hi=hi))
    return segments


def segment_by_samples(sorted_samples: list, num_segments: int) -> list[Segment]:
    """Cut a sampled, sorted set of key values into `num_segments` slices
    with roughly equal *sample* counts (quantile boundaries).

    Used for non-numeric keys (uuid, string) per spec §4.1 step 2: "segment
    by lexical ranges using sampled key percentiles". Because boundaries
    come from the actual observed distribution of keys rather than
    arithmetic on values, this balances correctly even for keys with no
    meaningful "midpoint" (like a UUID).
    """
    if not sorted_samples:
        return [Segment(index=0, lo=None, hi=None)]

    n = len(sorted_samples)
    if n < num_segments:
        num_segments = max(1, n)

    boundaries = []
    for i in range(1, num_segments):
        pos = math.floor(i * n / num_segments)
        pos = min(pos, n - 1)
        boundaries.append(sorted_samples[pos])

    # De-duplicate consecutive equal boundaries (low-cardinality samples
    # can otherwise produce a zero-width, meaningless segment).
    deduped: list = []
    for b in boundaries:
        if not deduped or deduped[-1] != b:
            deduped.append(b)

    lo_values = [sorted_samples[0], *deduped]
    hi_values: list = [*deduped, None]

    return [
        Segment(index=i, lo=lo, hi=hi)
        for i, (lo, hi) in enumerate(zip(lo_values, hi_values))
    ]


def clamp_to_range(segments: list[Segment], lo, hi) -> list[Segment]:
    """Force the first segment's lo (and, when `hi` is given, the last
    segment's hi) to the caller's already-known exact boundary.

    `segment_by_samples()`'s boundaries come from a *sample*, not the full
    table, so its first `lo` is only ever the smallest value that happened
    to be drawn — it can be strictly greater than the table's true minimum.
    Left uncorrected, any real key strictly between the true min and the
    sampled min satisfies no segment's `>= lo` at all (the first segment is
    the only one without a preceding sibling to catch it) and is silently
    skipped by every downstream query, on both sides equally, which is
    exactly how a real difference in that gap goes unreported.

    `hi=None` means "top-level, open-ended" (spec §4.1 step 2's last
    segment always stays open so new max-side inserts are still covered).
    A concrete `hi` means "bisecting an already-bounded parent segment"
    (spec §4.1 step 4): the parent's own hi is a hard boundary, so the
    last sub-segment must be capped there, never left open to spill into a
    sibling.
    """
    if not segments:
        return segments
    segments = list(segments)
    first = segments[0]
    if first.lo != lo:
        segments[0] = Segment(index=first.index, lo=lo, hi=first.hi)
    last = segments[-1]
    if hi is not None and last.hi != hi:
        segments[-1] = Segment(index=last.index, lo=last.lo, hi=hi)
    return segments


def assert_contiguous_coverage(segments: list[Segment], lo, hi) -> None:
    """Defensive check, cheap relative to the queries around it: segments
    must exactly tile `[lo, hi)` with no gap and no overlap, or some key
    range would be silently skipped (a real difference vanishes) or
    double-counted (a phantom one appears). `hi=None` asserts the last
    segment is open-ended rather than bounded at a specific value — see
    `clamp_to_range`'s docstring for when each applies.
    """
    assert segments, "segmentation produced no segments"
    assert segments[0].lo == lo, f"first segment lo {segments[0].lo!r} != range lo {lo!r}"
    assert segments[-1].hi == hi, f"last segment hi {segments[-1].hi!r} != range hi {hi!r}"
    for a, b in zip(segments, segments[1:]):
        assert a.hi == b.lo, f"gap/overlap between segments: hi={a.hi!r} next lo={b.lo!r}"
