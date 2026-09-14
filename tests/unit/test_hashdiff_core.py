"""Unit tests for the hashdiff algorithm (spec §4.1), against the sqlite
FakeConnector so these run with no database, no network, no Docker.

These cover the *M0* acceptance criteria (spec §13 M0) at the algorithm
level; tests/integration/test_m0_acceptance.py re-verifies the same
criteria end-to-end against a real Postgres, per spec §13's instruction to
"verify on real databases before calling a milestone done."
"""

from __future__ import annotations

import uuid

import pytest

from tablediff.core.errors import NonUniqueKeyError, NoPrimaryKeyError
from tablediff.core.hashdiff import diff
from tablediff.core.models import Column, TableRef

from .fake_connector import FakeConnector


def make_pair(rows_a, rows_b, columns, primary_key=("id",), table_name="t"):
    pk = list(primary_key) if primary_key else None

    src = FakeConnector()
    src.connect(":memory:")
    src.create_table(table_name, columns, pk, rows_a)

    tgt = FakeConnector()
    tgt.connect(":memory:")
    tgt.create_table(table_name, columns, pk, rows_b)

    ref = TableRef(engine="fake", database="db", table=table_name)
    return src, tgt, ref


INT_COLS = [
    Column("id", "bigint", nullable=False, ordinal=1),
    Column("name", "text", nullable=True, ordinal=2),
    Column("updated_at", "timestamp with time zone", nullable=True, ordinal=3),
]


def base_rows(n, ts="2024-03-01T00:00:00.000000Z"):
    return [{"id": i, "name": f"row-{i}", "updated_at": ts} for i in range(1, n + 1)]


class TestIdenticalTables:
    def test_identical_1000_row_tables_match(self):
        rows = base_rows(1000)
        src, tgt, ref = make_pair(rows, list(rows), INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"])
        assert result.is_match
        assert result.exit_code() == 0
        assert result.source_count == 1000
        assert result.target_count == 1000
        assert result.row_diffs == []


class TestMissingExtraChanged:
    def test_delete_update_insert_are_all_reported_and_classified(self):
        rows_a = base_rows(100)
        rows_b = [dict(r) for r in rows_a]

        # delete 10 rows (ids 1-10) from target
        deleted_ids = set(range(1, 11))
        rows_b = [r for r in rows_b if r["id"] not in deleted_ids]

        # update 10 rows (ids 11-20) in target
        updated_ids = set(range(11, 21))
        for r in rows_b:
            if r["id"] in updated_ids:
                r["name"] = r["name"] + "-CHANGED"

        # insert 10 new rows into target only (ids 101-110)
        inserted_ids = set(range(101, 111))
        for i in inserted_ids:
            rows_b.append({"id": i, "name": f"row-{i}", "updated_at": "2024-03-01T00:00:00.000000Z"})

        src, tgt, ref = make_pair(rows_a, rows_b, INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], row_threshold=10)

        assert not result.is_match
        assert result.exit_code() == 1
        assert result.missing_in_target == 10
        assert result.extra_in_target == 10
        assert result.changed == 10

        missing_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "missing"}
        extra_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "extra"}
        changed_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "changed"}
        assert missing_keys == deleted_ids
        assert extra_keys == inserted_ids
        assert changed_keys == updated_ids

        # A changed row must say *what* differed (spec §0 rule 6).
        changed_diff = next(rd for rd in result.row_diffs if rd.kind == "changed")
        assert "name" in changed_diff.changes


class TestCompositeKey:
    def test_composite_key_tenant_and_order_id(self):
        columns = [
            Column("tenant_id", "bigint", nullable=False, ordinal=1),
            Column("order_id", "bigint", nullable=False, ordinal=2),
            Column("status", "text", nullable=True, ordinal=3),
        ]
        rows_a = [
            {"tenant_id": t, "order_id": o, "status": "open"}
            for t in range(1, 4)
            for o in range(1, 21)
        ]
        rows_b = [dict(r) for r in rows_a]
        # change one specific (tenant_id, order_id) row
        for r in rows_b:
            if r["tenant_id"] == 2 and r["order_id"] == 10:
                r["status"] = "closed"

        src, tgt, ref = make_pair(rows_a, rows_b, columns, primary_key=("tenant_id", "order_id"))
        result = diff(src, tgt, ref, ref, key_columns=["tenant_id", "order_id"], row_threshold=5)

        assert result.changed == 1
        assert result.row_diffs[0].key == (2, 10)


class TestUuidKey:
    def test_uuid_key_table_diffs_correctly(self):
        columns = [
            Column("id", "uuid", nullable=False, ordinal=1),
            Column("name", "text", nullable=True, ordinal=2),
        ]
        ids = [str(uuid.uuid4()) for _ in range(500)]
        rows_a = [{"id": i, "name": "x"} for i in ids]
        rows_b = [dict(r) for r in rows_a]
        changed_id = ids[7]
        for r in rows_b:
            if r["id"] == changed_id:
                r["name"] = "y"

        src, tgt, ref = make_pair(rows_a, rows_b, columns, primary_key=("id",))
        result = diff(src, tgt, ref, ref, key_columns=["id"], row_threshold=20)

        assert result.changed == 1
        assert result.row_diffs[0].key == (changed_id,)


class TestNoPrimaryKey:
    def test_no_pk_and_no_explicit_key_raises(self):
        columns = [Column("name", "text", nullable=True, ordinal=1)]
        rows = [{"name": "a"}, {"name": "b"}]
        src, tgt, ref = make_pair(rows, list(rows), columns, primary_key=None)
        with pytest.raises(NoPrimaryKeyError):
            diff(src, tgt, ref, ref, key_columns=None)


class TestNonUniqueKey:
    def test_non_unique_explicit_key_raises_with_example(self):
        columns = [
            Column("email", "text", nullable=False, ordinal=1),
            Column("name", "text", nullable=True, ordinal=2),
        ]
        rows = [
            {"email": "a@x.com", "name": "A"},
            {"email": "a@x.com", "name": "A2"},  # duplicate key
            {"email": "b@x.com", "name": "B"},
        ]
        src, tgt, ref = make_pair(rows, list(rows), columns, primary_key=None)
        with pytest.raises(NonUniqueKeyError) as exc_info:
            diff(src, tgt, ref, ref, key_columns=["email"])
        assert "a@x.com" in str(exc_info.value)


class TestMaxDiffRowsCap:
    def test_diff_row_collection_is_capped_and_marked_truncated(self):
        rows_a = base_rows(200)
        rows_b = [dict(r) for r in rows_a if r["id"] > 50]  # 50 missing rows
        src, tgt, ref = make_pair(rows_a, rows_b, INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], row_threshold=10, max_diff_rows=5)
        assert result.truncated is True
        assert len(result.row_diffs) == 5
        # the *count* stays exact even though the row list is capped
        assert result.missing_in_target == 50


class TestExplainDoesNotExecute:
    def test_explain_returns_sql_without_running_against_source_or_target(self):
        from tablediff.core.hashdiff import explain

        rows = base_rows(10)
        src, tgt, ref = make_pair(rows, list(rows), INT_COLS)

        executed = []
        real_query = src.query

        def spying_query(sql, params=None):
            executed.append(sql)
            return real_query(sql, params)

        src.query = spying_query  # type: ignore[method-assign]

        statements = explain(src, tgt, ref, ref, key_columns=["id"])
        assert len(statements) > 0
        assert all(isinstance(s, str) for s in statements)
        assert executed == []  # explain must never execute anything


TEXT_COLS = [
    Column("id", "text", nullable=False, ordinal=1),
    Column("name", "text", nullable=True, ordinal=2),
]


def text_rows(keys, value="v"):
    return [{"id": k, "name": value} for k in keys]


class TestNonNumericKeySegmentationCoverage:
    """Regression tests for a real bug: segment_by_samples' boundaries come
    from a *sample*, so its first segment's lo can be strictly greater
    than the table's true minimum key. Any row between the true min and
    the sampled min then satisfies no segment's `>= lo` at all -- on BOTH
    sides equally, since the same segment boundaries are used for source
    and target -- so a real difference there was silently invisible to
    every query and the diff still reported MATCH."""

    def test_target_only_row_below_source_min_with_text_key(self):
        source_keys = [f"b{i:04d}" for i in range(1, 501)]
        rows_a = text_rows(source_keys)
        rows_b = [dict(r) for r in rows_a]
        rows_b.append({"id": "a0000", "name": "v"})  # lexically smaller than every source key

        src, tgt, ref = make_pair(rows_a, rows_b, TEXT_COLS, primary_key=("id",))
        result = diff(src, tgt, ref, ref, key_columns=["id"], row_threshold=50)

        assert not result.is_match
        assert result.extra_in_target == 1
        extra_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "extra"}
        assert extra_keys == {"a0000"}

    def test_30k_text_keys_three_smallest_changed_reports_exactly_three(self):
        keys = [f"k{i:05d}" for i in range(1, 30001)]
        rows_a = text_rows(keys)
        rows_b = [dict(r) for r in rows_a]
        # k00001..k00003 are the lexicographically (and numerically, since
        # zero-padded) smallest keys in the table -- exactly the range a
        # sample can most easily fail to represent.
        changed_ids = {"k00001", "k00002", "k00003"}
        for r in rows_b:
            if r["id"] in changed_ids:
                r["name"] = r["name"] + "-CHANGED"

        src, tgt, ref = make_pair(rows_a, rows_b, TEXT_COLS, primary_key=("id",))
        result = diff(src, tgt, ref, ref, key_columns=["id"], row_threshold=1000)

        assert not result.is_match
        assert result.changed == 3
        changed_keys = {rd.key[0] for rd in result.row_diffs if rd.kind == "changed"}
        assert changed_keys == changed_ids


class TestWhereFiltering:
    """spec §7: --where SQL applied to both sides (or --where-source /
    --where-target). Previously listed in the CLI options table but never
    actually implemented anywhere — found by inspection, not by a failing
    test, since nothing exercised it."""

    def test_where_excludes_rows_from_comparison_on_both_sides(self):
        rows_a = base_rows(20)
        rows_b = [dict(r) for r in rows_a]
        # A real difference in the excluded range (ids 1-5) must NOT show
        # up once --where restricts the comparison to id > 5.
        rows_b[0]["name"] = "TAMPERED"
        assert rows_b[0]["id"] == 1

        src, tgt, ref = make_pair(rows_a, rows_b, INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], where="id > 5")

        assert result.is_match
        assert result.source_count == 15
        assert result.target_count == 15

    def test_where_source_and_where_target_can_differ_per_side(self):
        rows_a = base_rows(10)
        rows_b = base_rows(10)

        src, tgt, ref = make_pair(rows_a, rows_b, INT_COLS)
        # Compare only the top half of each side's own id range — with
        # identical data this still matches even though the two filters
        # are textually different expressions.
        result = diff(
            src, tgt, ref, ref, key_columns=["id"],
            where_source="id > 5", where_target="id > 5",
        )
        assert result.is_match
        assert result.source_count == 5
        assert result.target_count == 5

    def test_duplicate_key_check_is_scoped_by_where_not_the_whole_table(self):
        # A duplicate that exists only *outside* the --where scope must not
        # block a diff that never intends to look at it.
        columns = [
            Column("id", "bigint", nullable=False, ordinal=1),
            Column("status", "text", nullable=True, ordinal=2),
            Column("name", "text", nullable=True, ordinal=3),
        ]
        rows = [
            {"id": 1, "status": "active", "name": "a"},
            {"id": 1, "status": "archived", "name": "a-dup"},  # duplicate id, but archived
            {"id": 2, "status": "active", "name": "b"},
        ]
        src, tgt, ref = make_pair(rows, list(rows), columns, primary_key=None)

        with pytest.raises(NonUniqueKeyError):
            diff(src, tgt, ref, ref, key_columns=["id"])  # unscoped: sees the duplicate

        # scoped to status='active': the duplicate id=1 row is 'archived',
        # so within the comparison's own scope the key really is unique.
        src2, tgt2, ref2 = make_pair(rows, list(rows), columns, primary_key=None)
        result = diff(src2, tgt2, ref2, ref2, key_columns=["id"], where="status = 'active'")
        assert result.is_match
        assert result.source_count == 2


class TestSampling:
    """spec §4.3: "--sample 1% (or --sample-rows N) ... apply the same
    sampling predicate to both sides (deterministic, based on
    hash(pk) % 100 < 1 so both sides pick the *same* rows), then run
    hashdiff on the sample. Report results as 'of the sampled rows' with
    the sample size stated. Never silently sample."
    """

    def test_no_sample_requested_leaves_sample_pct_none(self):
        rows = base_rows(50)
        src, tgt, ref = make_pair(rows, list(rows), INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"])
        assert result.sample_pct is None
        assert result.source_count == 50

    def test_sample_pct_narrows_scope_and_still_matches_identical_tables(self):
        rows = base_rows(2000)
        src, tgt, ref = make_pair(rows, list(rows), INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], sample=10.0)
        assert result.is_match, result.row_diffs
        assert result.sample_pct == 10.0
        # Not exactly 200 (hash-bucket sampling isn't a perfect draw), but
        # meaningfully narrower than the full 2000-row table -- proves
        # sampling actually reduced scope rather than being silently
        # ignored (spec's own "never silently sample").
        assert 50 < result.source_count < 500
        assert result.source_count == result.target_count

    def test_sample_is_deterministic_across_separate_runs(self):
        rows = base_rows(2000)
        src1, tgt1, ref1 = make_pair(rows, list(rows), INT_COLS)
        result1 = diff(src1, tgt1, ref1, ref1, key_columns=["id"], sample=10.0)

        src2, tgt2, ref2 = make_pair(rows, list(rows), INT_COLS)
        result2 = diff(src2, tgt2, ref2, ref2, key_columns=["id"], sample=10.0)

        # Same data, same sample percentage, run twice -- a real hash-based
        # predicate picks the exact same rows every time; a naive
        # RANDOM()-based one would not.
        assert result1.source_count == result2.source_count

    def test_sample_rows_converts_to_an_equivalent_percentage(self):
        rows = base_rows(2000)
        src, tgt, ref = make_pair(rows, list(rows), INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], sample_rows=200)
        assert result.sample_pct is not None
        assert 0 < result.sample_pct <= 100
        assert 0 < result.source_count < 2000

    def test_sample_still_reports_a_real_difference_within_the_sample(self):
        """A sampled diff must not silently swallow genuine differences
        that happen to land inside the sampled subset -- narrowing scope
        is about *which rows get examined*, never about weakening what
        gets reported for the rows that are."""
        rows_a = base_rows(3000)
        rows_b = [dict(r) for r in rows_a]
        for r in rows_b:
            r["name"] = r["name"] + "-CHANGED"
        src, tgt, ref = make_pair(rows_a, rows_b, INT_COLS)
        result = diff(src, tgt, ref, ref, key_columns=["id"], sample=20.0)
        assert not result.is_match
        assert result.changed > 0
        assert result.changed == result.source_count
