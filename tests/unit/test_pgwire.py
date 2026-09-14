"""Unit tests for the psql-stand-in's text->Python value casting.

_smart_cast is the one place the psql-subprocess connector (a stand-in for
psycopg — see docs/DEV_ENVIRONMENT.md) has to guess a value's type, since
psql only ever hands back text. These are fast, no-database checks of that
guess; tests/integration/test_m0_acceptance.py separately proves it against
a real column over the real subprocess path.
"""

from tablediff.connectors._pgwire import _smart_cast


def test_bare_integers_cast_to_int():
    assert _smart_cast("0") == 0
    assert _smart_cast("42") == 42
    assert _smart_cast("-7") == -7
    assert _smart_cast("+7") == 7


def test_leading_zero_values_are_left_as_text():
    # spec §6.1 INT-1: a genuine Postgres integer render never has a
    # leading zero, so a value that does can't really be one — it must be
    # a text column that merely looks numeric (zip code, external ID).
    assert _smart_cast("02139") == "02139"
    assert _smart_cast("-007") == "-007"


def test_empty_and_none_pass_through_unchanged():
    assert _smart_cast("") == ""
    assert _smart_cast(None) is None


def test_non_numeric_text_passes_through_unchanged():
    assert _smart_cast("hello") == "hello"
    assert _smart_cast("2024-03-01T00:00:00.000000Z") == "2024-03-01T00:00:00.000000Z"
