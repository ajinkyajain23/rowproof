"""Unit tests for core/segmentation.py (spec §4.1 step 2).

Pure math — no connectors involved. Two segmentation strategies:
  * segment_numeric_range   — dense/sparse numeric or orderable keys where
    we know min/max, so we cut the value range into equal-width slices.
  * segment_by_samples      — non-numeric keys (uuid/string) where we can't
    reason about the range, so we cut a *sampled, sorted* set of key values
    into slices with roughly equal sample counts (quantiles).
"""

import uuid

from rowproof.core.segmentation import (
    compute_num_segments,
    segment_by_samples,
    segment_numeric_range,
)


def test_compute_num_segments_respects_minimum_of_eight_for_small_tables():
    assert compute_num_segments(count=1000, target_rows_per_segment=100_000) == 8
    assert compute_num_segments(count=1, target_rows_per_segment=100_000) == 8


def test_compute_num_segments_scales_with_count():
    assert compute_num_segments(count=1_000_000, target_rows_per_segment=100_000) == 10
    # 850k rows / 100k target rounds up, not down, so no segment silently
    # absorbs the remainder above the target.
    assert compute_num_segments(count=850_000, target_rows_per_segment=100_000) == 9


def test_compute_num_segments_empty_table_is_one_segment():
    assert compute_num_segments(count=0, target_rows_per_segment=100_000) == 1


def test_segment_numeric_range_covers_full_range_with_no_gaps_or_overlap():
    segments = segment_numeric_range(min_val=1, max_val=1000, num_segments=8)
    assert len(segments) == 8
    assert segments[0].lo == 1
    assert segments[-1].hi is None  # last segment is open-ended (>= lo)
    # every value in [1, 1000] must fall in exactly one segment: adjacent
    # boundaries must be contiguous (next.lo == this.hi).
    for a, b in zip(segments, segments[1:]):
        assert a.hi == b.lo


def test_segment_numeric_range_single_value_table_is_one_segment_spanning_it():
    segments = segment_numeric_range(min_val=42, max_val=42, num_segments=8)
    assert len(segments) == 1
    assert segments[0].lo == 42
    assert segments[0].hi is None


def test_segment_numeric_range_balances_rows_for_dense_sequential_keys():
    # A 1..N sequential PK is the common case (bigserial). Simulate 1024
    # rows and check no numeric-range segment is more than 2x the average
    # — same balance bar the spec sets explicitly for UUID keys.
    n = 1024
    segments = segment_numeric_range(min_val=1, max_val=n, num_segments=8)
    counts = []
    for i, seg in enumerate(segments):
        hi = seg.hi if seg.hi is not None else n + 1
        counts.append(hi - seg.lo)
    avg = n / len(segments)
    assert max(counts) <= 2 * avg


def test_segment_by_samples_balances_uuid_keys():
    # No arithmetic works on UUIDs; segmentation must come from sampled,
    # sorted key percentiles instead (spec §4.1 step 2).
    keys = sorted(str(uuid.uuid4()) for _ in range(2000))
    segments = segment_by_samples(sorted_samples=keys, num_segments=8)
    assert len(segments) == 8

    # Simulate assigning every key to a segment by the same boundary rule
    # the connector's WHERE clause would use (lo <= key < hi), and check
    # no segment holds more than 2x the average — the M0 acceptance bar.
    def find_segment(key):
        for i, seg in enumerate(segments):
            if seg.hi is None or key < seg.hi:
                if key >= seg.lo:
                    return i
        raise AssertionError(f"key {key} not covered by any segment")

    buckets = [0] * len(segments)
    for k in keys:
        buckets[find_segment(k)] += 1

    avg = len(keys) / len(segments)
    assert max(buckets) <= 2 * avg
    assert min(buckets) > 0  # every segment should have gotten *some* rows


def test_segment_numeric_range_bounded_mode_never_exceeds_parent_hi():
    # This is the shape a recursive bisection call makes: splitting an
    # already-bounded sub-range (e.g. [2, 3), width 1) must not let the
    # last half fall back to open-ended, or it would swallow rows that
    # belong to the *next* sibling segment.
    segments = segment_numeric_range(min_val=2, max_val=2, num_segments=2, open_ended_last=False)
    assert all(s.hi is not None for s in segments)
    assert segments[-1].hi == 3  # max_val + 1, not None

    segments = segment_numeric_range(min_val=100, max_val=199, num_segments=4, open_ended_last=False)
    assert segments[-1].hi == 200
    for a, b in zip(segments, segments[1:]):
        assert a.hi == b.lo


def test_segment_by_samples_handles_duplicate_sample_values():
    # Low-cardinality sampled column (lots of repeats) shouldn't produce
    # empty or reversed ranges.
    keys = sorted(["a"] * 100 + ["b"] * 100 + ["c"] * 100)
    segments = segment_by_samples(sorted_samples=keys, num_segments=8)
    assert len(segments) >= 1
    for a, b in zip(segments, segments[1:]):
        assert a.hi is not None
        assert a.lo <= a.hi
        assert a.hi <= b.lo or a.hi == b.lo
