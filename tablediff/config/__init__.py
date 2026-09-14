# YAML config loading/validation for `tablediff run config.yaml` (spec §7,
# §9 M1).
"""Config file format (spec §7's `tablediff run CONFIG.yaml`):

    connections:
      prod_pg: postgres://user:${PROD_PG_PASSWORD}@host:5432/db
      analytics_ch: clickhouse://user:${CH_PASSWORD}@host:8123/db

    tables:
      - source: prod_pg/public.orders
        target: analytics_ch/orders
        key: order_id
        where: created_at > '2024-01-01'
      - source: prod_pg/public.customers
        target: analytics_ch/customers

A connection's value may also be `{dsn: ...}` for room to grow (e.g. a
future per-connection option) without breaking the plain-string form.
Every table entry accepts the same options `diff` does (key, columns,
exclude, where/where_source/where_target, algorithm, row_threshold,
max_diff_rows, trim, case_insensitive, float_precision, assume_tz,
column_map, fail_on) — `key`/`columns`/`exclude` accept either a YAML
list or a comma-separated string, matching the CLI flag's own grammar.

`${ENV_VAR}` is substituted everywhere in the raw file (spec §7:
"Secrets: accept ${ENV_VAR} in config and DSNs") before YAML parsing even
starts, so it works in a DSN, a `where` clause, or anywhere else — never
printed or logged: PostgresConnector's own query logging only ever sees
the substituted DSN inside a *DSN*, which _pgwire.py already never logs
(passwords go through PGPASSWORD, not the SQL/-c argument list).
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml

from tablediff.core.errors import TableDiffError
from tablediff.core.hashdiff import DEFAULT_MAX_DIFF_ROWS, DEFAULT_ROW_THRESHOLD

_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _substitute_env(text: str) -> str:
    def replace(m: re.Match) -> str:
        name = m.group(1)
        if name not in os.environ:
            raise TableDiffError(
                f"config references ${{{name}}} but that environment variable is not set"
            )
        return os.environ[name]

    return _ENV_VAR_RE.sub(replace, text)


def _as_list(value) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TableDiffError(f"expected a list or comma-separated string, got {value!r}")


def _as_column_map(value) -> dict | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return {str(k): str(v) for k, v in value.items()}
    if isinstance(value, str):
        result = {}
        for pair in value.split(","):
            pair = pair.strip()
            if not pair:
                continue
            src, _, tgt = pair.partition(":")
            result[src.strip()] = tgt.strip()
        return result or None
    raise TableDiffError(f"column_map: expected a mapping or 'a:b,c:d' string, got {value!r}")


@dataclass(frozen=True)
class TableJob:
    source: str
    target: str
    key: list[str] | None = None
    columns: list[str] | None = None
    exclude: list[str] | None = None
    where: str | None = None
    where_source: str | None = None
    where_target: str | None = None
    algorithm: str = "auto"
    row_threshold: int = DEFAULT_ROW_THRESHOLD
    max_diff_rows: int = DEFAULT_MAX_DIFF_ROWS
    trim: bool = False
    case_insensitive: bool = False
    float_precision: int = 15
    assume_tz: str = "UTC"
    column_map: dict | None = None
    fail_on: str = "any"


@dataclass(frozen=True)
class Config:
    connections: dict[str, str] = field(default_factory=dict)
    tables: list[TableJob] = field(default_factory=list)


def _resolve_connection_value(name: str, value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and "dsn" in value:
        return str(value["dsn"])
    raise TableDiffError(f"connections.{name}: expected a DSN string or a mapping with a 'dsn' key")


def load_config(path: str) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            raw_text = f.read()
    except OSError as e:
        raise TableDiffError(f"could not read config file '{path}': {e}") from e

    raw_text = _substitute_env(raw_text)
    try:
        data = yaml.safe_load(raw_text) or {}
    except yaml.YAMLError as e:
        raise TableDiffError(f"'{path}' is not valid YAML: {e}") from e
    if not isinstance(data, dict):
        raise TableDiffError(f"'{path}' must be a YAML mapping with 'connections'/'tables' keys")

    connections = {
        name: _resolve_connection_value(name, value)
        for name, value in (data.get("connections") or {}).items()
    }

    tables = []
    for i, t in enumerate(data.get("tables") or []):
        if not isinstance(t, dict) or "source" not in t or "target" not in t:
            raise TableDiffError(f"tables[{i}]: both 'source' and 'target' are required")
        tables.append(
            TableJob(
                source=t["source"],
                target=t["target"],
                key=_as_list(t.get("key")),
                columns=_as_list(t.get("columns")),
                exclude=_as_list(t.get("exclude")),
                where=t.get("where"),
                where_source=t.get("where_source"),
                where_target=t.get("where_target"),
                algorithm=t.get("algorithm", "auto"),
                row_threshold=t.get("row_threshold", DEFAULT_ROW_THRESHOLD),
                max_diff_rows=t.get("max_diff_rows", DEFAULT_MAX_DIFF_ROWS),
                trim=t.get("trim", False),
                case_insensitive=t.get("case_insensitive", False),
                float_precision=t.get("float_precision", 15),
                assume_tz=t.get("assume_tz", "UTC"),
                column_map=_as_column_map(t.get("column_map")),
                fail_on=t.get("fail_on", "any"),
            )
        )

    return Config(connections=connections, tables=tables)
