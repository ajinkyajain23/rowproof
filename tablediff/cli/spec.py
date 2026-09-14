"""Parsing for the SOURCE/TARGET connection+table grammar (spec §7):

    postgres://user:pw@host:5432/db/public.orders

i.e. a normal DSN, plus one extra path segment naming `schema.table` (or
just `table`, schema defaults to "public"). This is intentionally the only
thing this module does — engine dispatch and the actual connection live in
connectors/.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlsplit

from tablediff.core.models import TableRef


@dataclass(frozen=True)
class ParsedSource:
    engine: str
    connect_dsn: str
    table_ref: TableRef


_ENGINE_DEFAULT_PORT = {"postgres": 5432, "postgresql": 5432}


def parse_source_spec(spec: str) -> ParsedSource:
    parts = urlsplit(spec)
    engine = parts.scheme
    if not engine:
        raise ValueError(
            f"'{spec}' doesn't look like a connection string "
            "(expected engine://user:pw@host:port/database/table)"
        )

    path_segments = [seg for seg in parts.path.split("/") if seg]
    if len(path_segments) < 2:
        raise ValueError(
            f"'{spec}' is missing the table: expected "
            "engine://user:pw@host:port/database/[schema.]table"
        )
    database = path_segments[0]
    table_spec = "/".join(path_segments[1:])

    if "." in table_spec:
        schema, table = table_spec.split(".", 1)
    else:
        schema, table = None, table_spec

    netloc = parts.netloc
    connect_dsn = f"{parts.scheme}://{netloc}/{database}"

    return ParsedSource(
        engine=engine,
        connect_dsn=connect_dsn,
        table_ref=TableRef(engine=engine, database=database, schema=schema, table=table),
    )


def resolve_source_spec(spec: str, connections: dict[str, str] | None) -> ParsedSource:
    """spec §7: SOURCE/TARGET is either a full connection string, "or a
    named connection from the config file: prod_pg/public.orders" — used
    by `tablediff run` (and `cli.main.cmd_diff`, so `tablediff diff` can
    reference a config's connections too, once one is loaded). A named
    connection's own value is a bare DSN with no table (validated by
    config.load_config); this just splices the two together and reuses
    parse_source_spec's own DSN+table grammar rather than duplicating it.
    """
    if "://" in spec:
        return parse_source_spec(spec)
    if not connections:
        raise ValueError(
            f"'{spec}' doesn't look like a connection string and no named "
            "connections were loaded (expected engine://... or name/[schema.]table)"
        )
    name, _, rest = spec.partition("/")
    if name not in connections:
        raise ValueError(f"unknown connection '{name}' (known: {', '.join(sorted(connections))})")
    if not rest:
        raise ValueError(f"'{spec}' is missing the table: expected {name}/[schema.]table")
    return parse_source_spec(f"{connections[name]}/{rest}")
