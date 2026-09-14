"""Unit tests for the joindiff algorithm (spec §4.2) — single-query FULL
OUTER JOIN comparison, against the same fake-connector-over-sqlite harness
core/hashdiff.py's unit tests use (see tests/unit/fake_connector.py's
docstring for why a real-SQL fake beats a hand-rolled interpreter).

Joindiff's precondition is "same connection" — unlike hashdiff's tests,
which always use two separate FakeConnector instances (two separate
in-memory sqlite databases, modelling two independent connections), these
tests deliberately use ONE FakeConnector/one sqlite connection holding
both tables, passed as both `source` and `target`, exactly matching what
"same database" means for joindiff in production.
"""

from tablediff.core.joindiff import diff
from tablediff.core.models import Column, TableRef
from .fake_connector import FakeConnector

COLUMNS = [
    Column(name="id", native_type="bigint", nullable=False, ordinal=1),
    Column(name="name", native_type="text", nullable=True, ordinal=2),
]


def ref(table: str) -> TableRef:
    return TableRef(engine="fake", database="db", table=table)


def make_conn() -> FakeConnector:
    c = FakeConnector()
    c.connect("fake://db")
    return c


def rows(keys, value="v"):
    return [{"id": k, "name": f"{value}-{k}"} for k in keys]


def test_identical_tables_match():
    conn = make_conn()
    conn.create_table("a", COLUMNS, ["id"], rows(range(1, 21)))
    conn.create_table("b", COLUMNS, ["id"], rows(range(1, 21)))
    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"])
    assert result.is_match
    assert result.source_count == result.target_count == 20
    assert result.missing_in_target == 0
    assert result.extra_in_target == 0
    assert result.changed == 0


def test_missing_extra_and_changed_all_detected_in_one_query():
    conn = make_conn()
    src_rows = rows(range(1, 11))  # ids 1..10
    tgt_rows = rows(range(2, 12))  # ids 2..11: id=1 missing, id=11 extra
    tgt_rows[0] = {"id": 2, "name": "CHANGED"}  # id=2 changed
    conn.create_table("a", COLUMNS, ["id"], src_rows)
    conn.create_table("b", COLUMNS, ["id"], tgt_rows)

    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"])

    assert not result.is_match
    assert result.missing_in_target == 1
    assert result.extra_in_target == 1
    assert result.changed == 1
    kinds = {rd.kind: rd.key for rd in result.row_diffs}
    assert kinds["missing"] == (1,)
    assert kinds["extra"] == (11,)
    assert kinds["changed"] == (2,)
    assert result.row_diffs[[rd.kind for rd in result.row_diffs].index("changed")].changes["name"][:2] == (
        "v-2", "CHANGED",
    )


def test_algorithm_field_is_joindiff():
    conn = make_conn()
    conn.create_table("a", COLUMNS, ["id"], rows([1]))
    conn.create_table("b", COLUMNS, ["id"], rows([1]))
    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"])
    from tablediff.core.models import Algorithm
    assert result.algorithm is Algorithm.JOINDIFF


def test_where_filters_both_sides():
    conn = make_conn()
    conn.create_table("a", COLUMNS, ["id"], rows(range(1, 11)))
    conn.create_table("b", COLUMNS, ["id"], rows(range(1, 11)))
    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"], where="id <= 5")
    assert result.is_match
    assert result.source_count == 5
    assert result.target_count == 5


def test_max_diff_rows_caps_row_list_but_counts_stay_exact():
    conn = make_conn()
    conn.create_table("a", COLUMNS, ["id"], rows(range(1, 51)))
    conn.create_table("b", COLUMNS, ["id"], [])
    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"], max_diff_rows=5)
    assert result.missing_in_target == 50
    assert len(result.row_diffs) == 5
    assert result.truncated


def test_all_rows_differ_client_stays_bounded():
    # Every single row is "changed" (not missing/extra) — the case most
    # likely to tempt an implementation into fetching the whole result
    # set and capping in Python after the fact, since there's no obvious
    # smaller query to run instead. Proves both the reported numbers
    # (exact counts, capped list) AND — by spying on what actually
    # crossed the connector boundary — that the SQL itself is what
    # bounds the client, not a Python-side discard after pulling
    # everything.
    conn = make_conn()
    n = 500
    conn.create_table("a", COLUMNS, ["id"], rows(range(1, n + 1), value="v"))
    conn.create_table("b", COLUMNS, ["id"], rows(range(1, n + 1), value="CHANGED"))

    seen_row_counts = []
    real_query = conn.query

    def spying_query(sql, params=None):
        result_rows = real_query(sql, params)
        seen_row_counts.append(len(result_rows))
        return result_rows

    conn.query = spying_query

    max_diff_rows = 17
    result = diff(conn, conn, ref("a"), ref("b"), key_columns=["id"], max_diff_rows=max_diff_rows)

    assert result.changed == n  # exact, despite the cap
    assert result.missing_in_target == 0
    assert result.extra_in_target == 0
    assert len(result.row_diffs) == max_diff_rows
    assert result.truncated
    # No single query the algorithm issued returned more than
    # max_diff_rows + 1 rows of *diff detail* (the SQL LIMIT is
    # max_diff_rows + 1 — one extra row as an independent truncation
    # signal alongside the exact COUNT query, see joindiff.diff's
    # comment) — the counts query returns one row (three aggregate
    # columns), never n rows. The row list actually *stored* is still
    # capped at exactly max_diff_rows (checked above).
    assert max(seen_row_counts) <= max_diff_rows + 1
