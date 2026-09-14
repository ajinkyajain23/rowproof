from tablediff.cli.spec import parse_source_spec


def test_parses_schema_and_table():
    p = parse_source_spec("postgres://user:pw@host:5432/db/public.orders")
    assert p.engine == "postgres"
    assert p.connect_dsn == "postgres://user:pw@host:5432/db"
    assert p.table_ref.database == "db"
    assert p.table_ref.schema == "public"
    assert p.table_ref.table == "orders"


def test_table_without_explicit_schema_defaults_to_none():
    p = parse_source_spec("postgres://user:pw@host:5432/db/orders")
    assert p.table_ref.schema is None
    assert p.table_ref.table == "orders"


def test_missing_table_raises_clear_error():
    import pytest

    with pytest.raises(ValueError):
        parse_source_spec("postgres://user:pw@host:5432/db")
