# Dev environment note (read this once)

M0/M1/early M2 were built inside a sandboxed cloud dev environment with
**no general internet egress** — PyPI, npm and most apt mirrors were
blocked by org network policy, and there was no reachable ClickHouse at
any layer (no Docker daemon, no pre-installed binary). That ruled out
`pip install psycopg clickhouse-connect typer rich testcontainers` and
running anything against a real ClickHouse. Four narrowly-scoped
stand-ins were used instead, each sitting behind the exact interface the
real dependency would occupy — see git history before this note's last
edit for the full original stand-in table.

**Status as of this machine (Docker Desktop + real network access):**

| Spec choice | Status |
|---|---|
| `psycopg` (v3) for Postgres | **Done.** `rowproof/connectors/_pgwire.py` talks real wire-protocol Postgres; the old `psql`-subprocess stand-in is gone. |
| `clickhouse-connect` for ClickHouse | **Done.** `rowproof/connectors/_chwire.py` talks the real driver; the old stdlib-`urllib` client is gone. `tests/integration/test_clickhouse_real.py` runs for real (no longer skipped) against the ClickHouse container `docker-compose.yml` starts — see docs/LAPTOP_SETUP.md. |
| `typer` for `cli/main.py` | **Not done.** Still `argparse` — out of scope for the M2 connector work; CLI *behavior* (flags, exit codes, output) already matches spec §7 exactly, so this remains a pure rendering-layer swap whenever it's picked up. |
| `rich` for `cli/render.py` | **Not done.** Still plain `str.format` tables, same reasoning as above. |
| `testcontainers` for integration tests | **Not literally adopted** — `tests/integration/conftest.py` and `tests/integration/test_clickhouse_real.py` instead point directly at the Postgres/ClickHouse containers `docker-compose.yml` starts, via env vars with defaults matching that file (`ROWPROOF_TEST_PG_ADMIN_DSN`, `ROWPROOF_TEST_CH_DSN`). Same underlying goal (a real, disposable database per test run) reached a different way; revisit only if CI needs to spin databases up itself. |

Both connectors' public methods (`connect`, `query`, `get_schema`,
`get_primary_key`, `normalise_expr`, `row_hash_expr`, `aggregate_hash_expr`,
`quote_identifier`, `quote_literal`) kept their exact shape through the
swap — `core/` has zero database imports either way (spec rule 0.4), and
nothing about the algorithm or SQL generated changed because of it.

Two real bugs only surfaced once ClickHouse's SQL actually ran against a
server (both fixed, see `rowproof/connectors/clickhouse.py`'s
`_dec1_expr` docstring for detail): `toString(Decimal)` silently strips
trailing zeros, and `%` on a wide `Decimal256` doesn't behave like integer
modulo. Exactly the class of bug the "no new stand-ins past M2" rule
exists to catch early instead of shipping.
