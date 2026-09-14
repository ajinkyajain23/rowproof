# Handover — tablediff

You are taking over the `tablediff` project. Read this file, then `SPEC.md`, before doing anything.

## What this project is

An open-source Python CLI that verifies two database tables match, across engines (Postgres, ClickHouse, Snowflake). Full spec in `SPEC.md`. Follow it exactly; §0 has the rules.

## Where things stand

- **M0** (Postgres ↔ Postgres hashdiff): done, accepted.
- **M1** (normalisation rules, joindiff, JSON, config `run`): done, accepted.
- **Pre-M2 fixes** (TS-2 rounding, joindiff row cap, NULL rendering): done.
- **M2** (ClickHouse): code written, **never run against a real ClickHouse**. Not accepted. `tests/integration/test_clickhouse_real.py` has 8 tests that skip when no server is reachable.

Last known test run: 197 passed, 8 skipped (the ClickHouse ones).

## Known debt — fix first

The code was built in a sandbox with no internet, so three stand-ins were used instead of the real dependencies. Replace all of them now; no more stand-ins from here on:

| Stand-in | Replace with |
|---|---|
| `tablediff/connectors/_pgwire.py` (shells out to `psql`) | `psycopg` v3 |
| `tablediff/connectors/_chwire.py` (hand-rolled HTTP client) | `clickhouse-connect` |
| `argparse` in `cli/main.py`, plain text in `cli/render.py` | `typer` and `rich` |

`docs/DEV_ENVIRONMENT.md` describes the stand-ins; delete that file once they are gone.

Also not yet done for M2: segment translation for ClickHouse `UInt` keys, and the 100M-row benchmark.

## Environment on this machine

- Docker Desktop is installed and running.
- `docker-compose.yml` in this folder starts Postgres 16 (port 5432, user/pass `postgres`/`postgres`, db `td`) and ClickHouse 24.8 (HTTP port 8123, user/pass `default`/`clickhouse`).
- Internet access is normal: `pip install` works.

## Your task, in order

1. `docker compose up -d` and wait until both services are healthy.
2. Create a venv and install: `pip install -e .[dev] "psycopg[binary]" clickhouse-connect typer rich pytest testcontainers`. Update `pyproject.toml` so these are real dependencies (Snowflake stays an optional extra).
3. Replace the three stand-ins listed above. Keep the `Connector` interface (SPEC §5) unchanged.
4. Point `tests/integration/conftest.py` at the Docker databases (env vars with sensible defaults matching `docker-compose.yml`).
5. Run the full suite. The 8 ClickHouse tests must now run for real. Fix what fails. Lowest-confidence areas per the previous engineer: `_flt1_expr`, `_dec1_expr`, `_ts1_expr`/`_ts2_expr` (pre-1970 dates), and `row_hash_expr` on ClickHouse.
6. Wire ClickHouse `UInt` key segmentation into hashdiff.
7. Complete every M2 acceptance criterion in SPEC §13, including the cross-engine hash-equality test and the 100M-row benchmark (record it in `docs/benchmarks.md`).
8. Commit at each green step with a clear message.

## Rules

- Tests first, then code, then run the full suite. Never report a milestone done with a failing or skipped test.
- Never write to a user database (SPEC §0.5). Test fixtures may create tables; connectors may not.
- When done, paste the complete `pytest -v` output and a list of every M2 acceptance criterion marked PASS / FAIL.
- Stop after M2. Do not start M3 (Snowflake) without being told.
