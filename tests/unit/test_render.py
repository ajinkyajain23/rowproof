"""Unit tests for cli/render.py's row-output display layer.

NULL-1's canonical string is the literal `\\N` (spec §6.1) — that's what
every normalise_expr wraps a NULL in for hashing/comparison, and it must
stay that exact string everywhere a diff gets computed. But a human
reading a report should see "NULL", not a raw escape-looking `\\N` — this
file proves that swap happens in the *display* layer only, on an exact
match, never as a substring replace that could mangle a real value.
"""

import json

from rowproof.cli.render import render_json, render_terminal, _display
from rowproof.core.models import Algorithm, DiffResult, NormalisationRule, RowDiff, TableRef


def test_display_maps_the_null_1_marker_to_null():
    assert _display("\\N") == "NULL"


def test_display_leaves_other_values_untouched():
    assert _display("abc") == "abc"
    assert _display("") == ""
    assert _display("contains \\N inside a longer string") == "contains \\N inside a longer string"
    assert _display(0) == 0
    assert _display(None) is None


def _sample_result(sv, tv) -> DiffResult:
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="postgres", database="db", table="b"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=1,
        target_count=1,
        changed=1,
    )
    result.row_diffs.append(
        RowDiff(key=(1,), kind="changed", changes={"name": (sv, tv, NormalisationRule.STR_1)})
    )
    return result


def test_terminal_output_shows_null_not_backslash_n():
    result = _sample_result("\\N", "hello")
    text = render_terminal(result)
    assert "NULL" in text
    assert "\\N" not in text


def test_json_output_shows_null_not_backslash_n():
    result = _sample_result("\\N", "hello")
    payload = render_json(result)
    assert '"source": "NULL"' in payload
    assert "\\\\N" not in payload  # the JSON-escaped form of a literal \N


def test_real_backslash_n_in_the_middle_of_a_value_is_not_mangled():
    # "a\Nb" is not the NULL-1 marker (that's the exact string "\N"), so it
    # must reach the output as itself — repr()'d like any other string
    # value, never swapped to "NULL" or otherwise touched by _display.
    result = _sample_result("a\\Nb", "hello")
    text = render_terminal(result)
    assert repr("a\\Nb") in text
    assert "NULL" not in text


def test_terminal_shows_sampled_notice_with_percentage_when_sample_pct_set():
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="postgres", database="db", table="b"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=50,
        target_count=50,
        sample_pct=5.0,
    )
    text = render_terminal(result)
    assert "SAMPLED" in text
    assert "5%" in text


def test_terminal_omits_sampled_notice_when_sample_pct_is_none():
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="postgres", database="db", table="b"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=50,
        target_count=50,
    )
    text = render_terminal(result)
    assert "SAMPLED" not in text


def test_json_carries_sample_pct_field():
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="postgres", database="db", table="b"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=50,
        target_count=50,
        sample_pct=2.5,
    )
    payload = render_json(result)
    assert '"sample_pct": 2.5' in payload


def test_json_carries_tool_name():
    # M4 rename: JSON output identifies itself as "rowproof".
    result = DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="postgres", database="db", table="b"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=50,
        target_count=50,
    )
    payload = json.loads(render_json(result))
    assert payload["tool"] == "rowproof"
    assert payload["schema_version"] == 1


def _match_with_excluded(excluded):
    return DiffResult(
        source=TableRef(engine="postgres", database="db", table="a"),
        target=TableRef(engine="clickhouse", database="db", table="a"),
        key_columns=("id",),
        algorithm=Algorithm.HASHDIFF,
        source_count=10,
        target_count=10,
        excluded_columns=list(excluded),
    )


def test_terminal_match_that_skipped_columns_says_so_in_the_verdict():
    # Found migrating a real database: `MATCH exit 0` was printed while a
    # column was never compared (type mismatch -> excluded). For a tool that
    # exists to give proof, the headline must not read as a clean pass.
    text = render_terminal(_match_with_excluded(["activebool", "picture"]))
    verdict = [line for line in text.splitlines() if line.strip().startswith("result")][0]
    assert "MATCH" in verdict
    assert "NOT compared" in verdict
    assert "2 column" in verdict


def test_terminal_clean_match_verdict_is_unchanged():
    text = render_terminal(_match_with_excluded([]))
    verdict = [line for line in text.splitlines() if line.strip().startswith("result")][0]
    assert verdict.split() == ["result", "MATCH", "exit", "0"]


def test_json_says_whether_every_column_was_compared():
    full = json.loads(render_json(_match_with_excluded([])))
    partial = json.loads(render_json(_match_with_excluded(["x"])))
    assert full["fully_compared"] is True
    assert partial["fully_compared"] is False
    assert partial["excluded_columns"] == ["x"]
    assert partial["is_match"] is True  # exit code semantics unchanged
