"""Unit tests for YAML config loading (spec §7's `tablediff run
CONFIG.yaml`) — pure Python, no database, no CLI.
"""


import pytest

from tablediff.config import load_config
from tablediff.core.errors import TableDiffError


def write(tmp_path, name, content):
    p = tmp_path / name
    p.write_text(content)
    return str(p)


def test_loads_connections_and_tables(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections:
          prod_pg: postgres://user:pw@host:5432/db
        tables:
          - source: prod_pg/public.orders
            target: prod_pg/public.orders_replica
            key: order_id
        """,
    )
    config = load_config(path)
    assert config.connections == {"prod_pg": "postgres://user:pw@host:5432/db"}
    assert len(config.tables) == 1
    job = config.tables[0]
    assert job.source == "prod_pg/public.orders"
    assert job.target == "prod_pg/public.orders_replica"
    assert job.key == ["order_id"]


def test_connection_as_mapping_with_dsn_key(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections:
          prod_pg:
            dsn: postgres://user:pw@host:5432/db
        tables: []
        """,
    )
    config = load_config(path)
    assert config.connections == {"prod_pg": "postgres://user:pw@host:5432/db"}


def test_env_var_substitution_in_dsn(tmp_path, monkeypatch):
    monkeypatch.setenv("TEST_PG_PASSWORD", "s3cret")
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections:
          prod_pg: postgres://user:${TEST_PG_PASSWORD}@host:5432/db
        tables: []
        """,
    )
    config = load_config(path)
    assert config.connections["prod_pg"] == "postgres://user:s3cret@host:5432/db"


def test_missing_env_var_raises_clear_error(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections:
          prod_pg: postgres://user:${DEFINITELY_NOT_SET_XYZ}@host:5432/db
        tables: []
        """,
    )
    with pytest.raises(TableDiffError, match="DEFINITELY_NOT_SET_XYZ"):
        load_config(path)


def test_table_missing_source_or_target_raises(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections: {}
        tables:
          - source: prod_pg/public.orders
        """,
    )
    with pytest.raises(TableDiffError, match="source.*target|target.*source"):
        load_config(path)


def test_key_columns_accept_comma_separated_string_or_list(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections: {}
        tables:
          - source: a://x/db/t1
            target: a://x/db/t2
            key: tenant_id, order_id
          - source: a://x/db/t3
            target: a://x/db/t4
            key: [id]
        """,
    )
    config = load_config(path)
    assert config.tables[0].key == ["tenant_id", "order_id"]
    assert config.tables[1].key == ["id"]


def test_missing_file_raises_table_diff_error(tmp_path):
    with pytest.raises(TableDiffError):
        load_config(str(tmp_path / "does-not-exist.yaml"))


def test_defaults_applied_when_options_omitted(tmp_path):
    path = write(
        tmp_path,
        "config.yaml",
        """
        connections: {}
        tables:
          - source: a://x/db/t1
            target: a://x/db/t2
        """,
    )
    job = load_config(path).tables[0]
    assert job.algorithm == "auto"
    assert job.fail_on == "any"
    assert job.trim is False
    assert job.float_precision == 15
    assert job.assume_tz == "UTC"
