"""Unit tests for cli/render.py's row-output display layer.

NULL-1's canonical string is the literal `\\N` (spec §6.1) — that's what
every normalise_expr wraps a NULL in for hashing/comparison, and it must
stay that exact string everywhere a diff gets computed. But a human
reading a report should see "NULL", not a raw escape-looking `\\N` — this
file proves that swap happens in the *display* layer only, on an exact
match, never as a substring replace that could mangle a real value.
"""

from tablediff.cli.render import render_json, render_terminal, _display
from tablediff.core.models import Algorithm, DiffResult, NormalisationRule, RowDiff, TableRef


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
