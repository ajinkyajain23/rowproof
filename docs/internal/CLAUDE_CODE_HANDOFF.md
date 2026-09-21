# Handoff prompt for Claude Code

This project moved from a cloud sandbox (no Docker, no real ClickHouse, no
general internet) to a real machine. From here on, **Claude Code runs this
project itself** — it has real shell access, so it should bring the
databases up, install real drivers, and carry M2 to a genuinely green,
unskipped suite, not just react to pasted pytest output.

Give Claude Code this whole file as its instruction (e.g. "read
`docs/internal/CLAUDE_CODE_HANDOFF.md` in this folder and follow it exactly").

## 0. Orientation

Read `HANDOVER.md` and `SPEC.md` first — `SPEC.md` §0 has the project's
non-negotiable rules, most importantly:

- Tests first for every acceptance criterion, then implement until green,
  then run the full suite and show the output. Never report a milestone
  done with any failing or skipped test.
- Connectors are read-only by construction — never write to a user
  database (SPEC §0.5); only test fixtures may create tables.
- **Real drivers only from M2 onward — no new stand-ins**, however
  inconvenient. `docs/internal/DEV_ENVIRONMENT.md` explains the two stand-ins this
  project currently has (`_pgwire.py` shells out to `psql`; `_chwire.py` is
  a real stdlib HTTP client that has never been run against a real
  server) and exactly what replacing each one requires.
- Stop after M2. Do not start M3 (Snowflake) without being told.

## 1. Bring up the databases

```
docker info                 # confirm Docker Desktop is running
docker compose up -d
docker compose ps           # wait until both services show "healthy"
```

`docker-compose.yml` in this folder starts Postgres 16 (port 5432, user/pass
`postgres`/`postgres`, an extra `td` database — the tests use the
always-present `postgres` maintenance database as their admin DSN, so `td`
being a different name than earlier drafts doesn't matter) and ClickHouse
24.8 (HTTP port 8123, user `default`, **password `clickhouse`**).

**Important:** the test suite's built-in default ClickHouse DSN assumes no
password (`clickhouse://default:@127.0.0.1:8123/default`). This
compose file sets one, so export the override before running pytest:

```
export ROWPROOF_TEST_CH_DSN="clickhouse://default:clickhouse@127.0.0.1:8123/default"
```

(Postgres needs no override — `ROWPROOF_TEST_PG_ADMIN_DSN` already
defaults to `postgres://postgres:postgres@127.0.0.1:5432/postgres`, which
this compose file satisfies.)

## 2. Git

If this folder isn't already a git repo, initialize one and make an
initial commit of the imported code before changing anything:

```
git init
git add -A
git commit -m "Import M0-M2 code from cloud session"
```

Commit again at each green step from here on, with a clear message.

## 3. Install real dependencies

```
python3 -m venv .venv && source .venv/bin/activate   # or your usual venv tool
pip install -e ".[dev]"
pip install "psycopg[binary]>=3.1" clickhouse-connect typer rich testcontainers
```

Update `pyproject.toml` so these become real dependencies, not just
something installed ad hoc — Snowflake stays an optional extra (M3 hasn't
started). `pip install -e ".[dev]"` already picks up pytest/ruff/pyyaml/
jinja2 from `pyproject.toml`.

## 4. Run the full suite and read the real failures

```
pytest -v
```

The 8 tests in `tests/integration/test_clickhouse_real.py` should now
execute for real instead of skipping (module docstring there explains why
they were skipping before). Expect some failures — treat every one as a
genuine bug report, not noise. Known low-confidence areas going in (from
the previous engineer, not a complete list — trust the real failures over
this):

- `_dec1_expr` — banker's-rounding arithmetic
- `_flt1_expr` — scientific-notation construction
- `_ts1_expr` / `_ts2_expr` — pre-1970 dates (known negative-modulo bug)
- `row_hash_expr` on ClickHouse generally (little-endian vs. Postgres's
  big-endian `reinterpretAsInt64`/`bit(64)::bigint` — see the `reverse()`
  call in `clickhouse.py` and the cross-checked reference tests in
  `tests/unit/test_clickhouse_hash_reference.py`)

## 5. Replace the two stand-ins

- `rowproof/connectors/postgres.py`: replace `_pgwire.py`'s
  psql-subprocess calls with real `psycopg` (v3) calls.
- `rowproof/connectors/clickhouse.py`: replace `_chwire.py`'s stdlib-HTTP
  calls with real `clickhouse-connect` calls.

Neither connector's public method signatures should need to change
(`connect`, `query`, `get_schema`, `get_primary_key`, `normalise_expr`,
`row_hash_expr`, `aggregate_hash_expr`, `quote_identifier`,
`quote_literal`) — see `docs/internal/DEV_ENVIRONMENT.md`'s existing swap-out notes
for the exact contract each must keep. Once both are gone, also point
`tests/integration/conftest.py` at the Docker databases explicitly (env
vars with defaults matching `docker-compose.yml`) and delete
`docs/internal/DEV_ENVIRONMENT.md`'s stand-in table (per `HANDOVER.md`).

## 6. Finish what M2 is still missing

- ClickHouse `UInt` key segmentation: `core/hashdiff.py`'s segment-and-bisect
  path currently has no ClickHouse-specific key handling at all (SPEC §9).
  Write tests first, then wire it in.
- The 100M-row/5-minute/500MB benchmark from SPEC §13 M2's last acceptance
  line — run it, record the numbers in `docs/benchmarks.md`.
- Every other M2 acceptance criterion in SPEC §13, including the
  cross-engine hash-equality test (hash the same fixture row/string on
  both engines, assert equality).

## 7. When done

Run the full suite one more time and paste the complete `pytest -v`
output, plus a list of every M2 acceptance criterion from SPEC §13 marked
PASS or FAIL. M2 is done only when that list is all PASS and the suite is
green with nothing skipped. Then stop — M3 is a separate go-ahead.
