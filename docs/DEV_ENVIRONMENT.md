# Dev environment note (read this once)

M0 was built inside a sandboxed cloud dev environment with **no general
internet egress** — PyPI, npm and most apt mirrors were blocked by org
network policy (only Anthropic-internal hosts were reachable; the sandbox's
own base image already had Postgres 16 and a handful of CLI tools
pre-installed, and that's what M0 was built against).

That ruled out `pip install psycopg typer rich testcontainers` — none of
the spec's chosen stack (§3) could be installed. Rather than block on
network access, M0 was built with three narrowly-scoped stand-ins, each
sitting behind the exact interface the real dependency would occupy:

| Spec choice | Stand-in used | Where |
|---|---|---|
| `psycopg` (v3, wire protocol) | shells out to the `psql` CLI binary, parses `-A -t` output | `tablediff/connectors/_pgwire.py` |
| `typer` | `argparse` | `tablediff/cli/main.py` |
| `rich` | plain `str.format` tables | `tablediff/cli/render.py` |
| `testcontainers` (Docker-based Postgres for tests) | a real, natively-installed local Postgres 16, started once per test session | `tests/integration/conftest.py` |
| `clickhouse-connect` (M2) | a real stdlib `urllib` client for ClickHouse's plain HTTP interface — not a shim behind a CLI binary like `_pgwire.py` (no `clickhouse-client` was available to shell out to), but still **never executed against a real ClickHouse server** — this sandbox never had a reachable one at all (no Docker daemon, no apt/pip network access, no pre-installed binary; the linked device bridge was separately broken by an unrelated bug). See `tablediff_status_report.md` for exactly what is and isn't verified. | `tablediff/connectors/_chwire.py` |

**Postgres's stand-in was low-risk** — `_pgwire.py` talks to a real,
natively-installed local Postgres, so every Postgres-side test in this
project has always run against the real engine; only the *transport*
(subprocess vs. wire protocol) was substituted. **ClickHouse's stand-in is
different in kind, not just degree**: there was never a real ClickHouse to
run anything against, at any layer. `tests/integration/test_clickhouse_real.py`
exists and is written correctly, but every one of its tests is currently
*skipped*, not passing — see `docs/LAPTOP_SETUP.md` for closing that gap.

**Rule from M2 onward: real drivers only, no new stand-ins.** Two engines'
worth of substitutes is where subtle, engine-specific bugs start hiding
behind tests that only prove a Python function returns a plausible-looking
string. Postgres's transport-only stand-in stays (it's real-engine-backed
and already scheduled for removal per the swap-out list below); nothing
past M2 should follow ClickHouse's shape of "reasoned through, never run."

None of this touched `core/` — it has zero database imports either way, per
spec rule 0.4. The `Connector` protocol (§5) is implemented exactly as
specified; `PostgresConnector` happens to talk to the database via
subprocess instead of a socket library, but every method has the same
signature and the same contract, and it's covered by the same integration
tests any wire-protocol driver would need to pass.

**Before the v1.1 milestones / launch**, once this runs somewhere with
normal network access (your laptop, CI, a real Docker host):

1. `pip install psycopg[binary]` and replace `_pgwire.py`'s subprocess calls
   in `PostgresConnector` with real `psycopg` calls. The connector's public
   methods (`connect`, `query`, `get_schema`, `get_primary_key`,
   `normalise_expr`, `row_hash_expr`, `aggregate_hash_expr`,
   `quote_identifier`, `quote_literal`) don't need to change shape.
2. `pip install typer rich` and port `cli/main.py` / `cli/render.py`. The
   CLI's *behavior* (flags, exit codes, output content) was written to match
   §7–§8 of the spec exactly, so this is a rendering-layer swap, not a
   redesign.
3. Add `testcontainers[postgres]` and point `tests/integration/conftest.py`
   at a container instead of (or in addition to) a local instance — the
   fixture already isolates "how do I get a Postgres to point at" from the
   tests themselves, so this is a fixture-only change.
4. `pip install clickhouse-connect` and replace `_chwire.py`'s calls in
   `tablediff/connectors/clickhouse.py` with the real client — same
   contract, same method signatures. Critically, this is also the point
   where `tests/integration/test_clickhouse_real.py` finally runs for
   real instead of skipping; treat every failure it produces as a genuine
   bug report, not noise (see this file's ClickHouse row above for why
   confidence there is low).
5. Delete this file's stand-in table once all four are gone.

Nothing about the algorithm, the SQL generated, or the test coverage is
weaker because of this — it only affects *how Python talks to Postgres* and
*how the CLI parses flags / prints text*.
