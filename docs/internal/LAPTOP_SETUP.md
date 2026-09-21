# Moving off the sandbox: laptop setup

The cloud sandbox this project was built in has no path to a real
ClickHouse (see DEV_ENVIRONMENT.md) and, as of M2, no path to install real
drivers for Postgres either — every milestone so far has run against
stand-ins (`_pgwire.py` shells out to `psql`, `_chwire.py` is a real HTTP
client but has never talked to a real server). That's fine for M0/M1
(Postgres-only, stand-in verified against a real local install) but not
fine for M2's cross-engine claims. **From here on: real drivers only, no
new stand-ins**, however inconvenient a given environment is.

## 1. Docker

Install Docker Desktop (docker.com), start it, confirm it's running:

```
docker info
```

## 2. Start Postgres + ClickHouse

From the project root (where `docker-compose.yml` lives):

```
docker compose up -d
docker compose ps          # wait until both show "healthy"
```

The compose file's ports/credentials match this project's existing test
defaults exactly (`postgres://postgres:postgres@127.0.0.1:5432/postgres`,
`clickhouse://default:@127.0.0.1:8123/default`) — no env vars needed.

## 3. Get the code and install real dependencies

This project was never a git repo in the sandbox (no remote to clone) —
unzip the delivered archive instead:

```
unzip rowproof_m2_wip.zip -d rowproof && cd rowproof
python3 -m venv .venv && source .venv/bin/activate   # or your usual venv tool
pip install -e ".[dev]"
pip install "psycopg[binary]>=3.1" clickhouse-connect typer rich testcontainers
```

(`pip install -e ".[dev]"` picks up pytest/ruff/pyyaml/jinja2 already
declared in `pyproject.toml`; the second line adds the real drivers the
sandbox could never reach.)

## 4. Run the full suite

```
pytest -v
```

Expect the 8 tests in `tests/integration/test_clickhouse_real.py` to
finally execute for real instead of skipping. Per the status report,
expect some of them to fail — `_flt1_expr` (FLT-1 scientific notation) and
`_dec1_expr` (DEC-1 banker's rounding) are flagged lowest-confidence, and
`_ts1_expr`/`_ts2_expr` have a known unfixed pre-1970 negative-modulo bug.
A failure here is the system working as designed — it's exactly what
these tests exist to catch before M2 is ever called done.

## 5. Replace the stand-ins, then hand the rest to Claude Code

Once real Postgres/ClickHouse are reachable, `_pgwire.py`'s psql-subprocess
approach and `_chwire.py`'s untested-but-real HTTP client should both be
replaced with the actual drivers (`psycopg`, `clickhouse-connect`) — see
DEV_ENVIRONMENT.md's existing swap-out notes for `_pgwire.py`; the same
shape of change applies to `_chwire.py`. A suggested prompt for that whole
next phase is in this repo alongside this file: `CLAUDE_CODE_HANDOFF.md`.
