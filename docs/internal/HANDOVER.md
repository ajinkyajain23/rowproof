# Handover — rowproof

You are taking over the `rowproof` project. Read this file, then `SPEC.md`, before doing anything.

## What this project is

An open-source Python CLI that verifies two database tables match, across engines (Postgres, ClickHouse, Snowflake). Full spec in `SPEC.md`. Follow it exactly; §0 has the rules.

## Where things stand

- **M0** (Postgres ↔ Postgres hashdiff): done, accepted.
- **M1** (normalisation rules, joindiff, JSON, config `run`): done, accepted.
- **Pre-M2 fixes** (TS-2 rounding, joindiff row cap, NULL rendering): done.
- **M2** (ClickHouse): **done, accepted** — real `psycopg`/`clickhouse-connect`
  drivers, ClickHouse key segmentation (UInt/String/UUID), the
  cross-engine hash-equality test, and every other §13 M2 acceptance
  criterion pass against real Postgres 16 + ClickHouse 24.8 (Docker), with
  one documented, accepted exception: the 100M-row benchmark passes on
  correctness (exact difference counts) and memory (127.5 MB, budget
  500 MB) but takes 8.95 min against a 5-min target — root-caused to a
  real CPU-core ceiling on the shared 8-vCPU dev VM this ran on, not a
  software inefficiency (see `docs/benchmarks.md` for the full
  investigation and what would close the gap on better hardware).

Last known test run: 209 passed, 0 skipped.

## Known debt — carried forward, not required for M2

`argparse` in `cli/main.py` and plain-text tables in `cli/render.py` are
still stand-ins for `typer`/`rich` — spec §3's chosen stack, but not
required by any M2 acceptance criterion, so out of scope for the M2 pass.
CLI *behaviour* (flags, exit codes, output content) matches spec §7–§8
exactly regardless; this is a pure rendering-layer swap whenever it's
picked up. `docs/DEV_ENVIRONMENT.md` has the current, accurate status of
every stand-in.

## Environment on this machine

- Docker Desktop is installed and running.
- `docker-compose.yml` in this folder starts Postgres 16 (port 5432, user/pass `postgres`/`postgres`, db `td`) and ClickHouse 24.8 (HTTP port 8123, user/pass `default`/`clickhouse`).
- Internet access is normal: `pip install` works.
- `.venv` (Python 3.12) has every real dependency installed; activate it
  before running anything (`.venv\Scripts\python.exe` on Windows).

## Rules

- Tests first, then code, then run the full suite. Never report a milestone done with a failing or skipped test.
- Never write to a user database (SPEC §0.5). Test fixtures may create tables; connectors may not.
- M3 (Snowflake) has not been started. Do not start it without being told.
