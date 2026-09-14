# TableDiff — Implementation Spec

**Open-source CLI that verifies two tables match, across different database engines. Built for migrations and replication checks.**

Working name: `tablediff` (placeholder — check PyPI and GitHub availability, rename before launch).
Licence: Apache 2.0 for everything in this spec. The paid tier (§12) is a separate, closed repo and is **not** part of v1.
Target users: data engineers mid-migration (Postgres → ClickHouse, Snowflake → Databricks, warehouse → Iceberg) and teams running CDC replication.

---

## 0. Instructions for the implementing agent

Read this before writing any code.

1. **Correctness beats speed, speed beats features.** A diff that reports "match" when rows differ is the worst possible bug. It destroys the product's only asset, which is trust. Every normalisation rule in §6 must have a test before it is used.
2. **The databases do the work.** Never pull full tables to the client. All hashing, counting and aggregation is pushed down as SQL. The client only ever holds segment boundaries, hashes, and the final list of differing rows (capped).
3. **Build in milestone order (§9).** M0 must be a working Postgres ↔ Postgres diff before any second engine is added. Do not start on ClickHouse until the M0 acceptance criteria pass.
4. **Every engine-specific behaviour lives in that engine's connector.** The core algorithm (`core/`) must have zero imports from any database driver. It must be testable on the JVM-equivalent here: plain pytest with fake connectors.
5. **Never write to a user's database.** Connectors are read-only by construction: no DDL, no DML, no temp tables in v1. If an engine needs a temp table for performance, that is a v2 decision, opt-in via flag.
6. **Explain every mismatch.** When two values differ, the output must say what differed and, where a normalisation rule was involved, which rule. "Row 4711 differs" is not acceptable; "Row 4711: `updated_at` 2024-03-01T10:00:00Z vs 2024-03-01T10:00:00.000123Z (sub-second precision; see rule TS-2)" is.
7. Keep the dependency list small. Every dependency is something a user's security team will question.

---

## 1. The problem

A team migrates a table from database A to database B (or replicates A into B continuously). They must answer: **does B contain exactly what A contains?**

Today they either:
- write ad-hoc `SELECT COUNT(*), SUM(...)` checks per table, which miss row-level differences and don't scale to 200 tables;
- pull both tables into pandas, which fails past a few million rows;
- buy Datafold (enterprise, demo-only); or
- trust the migration tool's own logs, which report rows *sent*, not rows *correct*.

The open-source `data-diff` package solved this and was sunset in May 2024. Its users still exist. This tool replaces it, done better on the parts that broke: cross-engine type handling, clear explanations, and a report someone can sign off on.

---

## 2. Scope

### v1 — in

- `tablediff diff` between two tables, same or different engines
- Engines: **Postgres, ClickHouse, Snowflake** (in that order)
- Two algorithms: **hashdiff** (cross-engine) and **joindiff** (same engine, same database)
- Key-based comparison on a single or composite primary key
- Column subset and column exclusion
- Where-clause filtering on both sides
- Sampling mode for very large tables
- Normalisation rules for cross-engine type differences (§6)
- Output: terminal summary, JSON, and a self-contained **HTML sign-off report**
- Config file for repeated runs (many tables in one invocation)
- Exit codes usable in CI

### v1 — explicitly out

- Any write to a user database
- Schema diff (column names and types compare is v1.1, easy, but not now)
- Databricks, BigQuery, MySQL, DuckDB/Iceberg connectors — v1.1+
- Scheduling, history, alerts, web UI — that is the paid tier, separate repo
- Any LLM calls at runtime
- Fixing or syncing differences

---

## 3. Stack

| Concern | Choice | Note |
|---|---|---|
| Language | Python 3.11+ | Adoption in data engineering; the heavy work is SQL anyway |
| Packaging | `uv`, `pyproject.toml`, published to PyPI | `pipx install tablediff` must work |
| CLI | `typer` | |
| Terminal output | `rich` | Tables, progress bars |
| Postgres | `psycopg` (v3) | |
| ClickHouse | `clickhouse-connect` | |
| Snowflake | `snowflake-connector-python` | Heavy dependency; install as an extra: `tablediff[snowflake]` |
| Config | YAML via `pyyaml` | |
| HTML report | Jinja2 template, inline CSS, no external assets | Must open from a file share with no network |
| Tests | `pytest`, `testcontainers` for Postgres and ClickHouse | Snowflake tests need credentials; mark as `integration` and skip by default |
| Lint/format | `ruff` | |

### Module layout

```
tablediff/
  cli/            typer commands, output rendering
  core/           PURE PYTHON. Algorithms, segmentation, normalisation rules, result model. No DB imports.
  connectors/
    base.py       Connector interface (§5)
    postgres.py
    clickhouse.py
    snowflake.py
  report/         JSON + HTML rendering from the result model
  config/         YAML loading, validation
tests/
  unit/           core/ with fake connectors
  fixtures/       normalisation cases (§6.4)
  integration/    testcontainers-based
```

Every connector driver is an optional extra. Base install pulls in Postgres only.

---

## 4. Algorithms

### 4.1 Hashdiff (cross-engine, default)

Precondition: a primary key (single or composite) exists on both sides and its values are comparable after normalisation.

1. **Bounds.** `SELECT MIN(pk), MAX(pk), COUNT(*)` on both sides. If counts differ, record it but continue — the diff must still say *which* rows.
2. **Segment.** Split `[min, max]` into `N` segments (default target: ~100k rows per segment, computed from count, minimum 8 segments). For non-numeric keys (UUID, string), segment by lexical ranges using sampled key percentiles (`N` quantiles from a sampled `SELECT pk ... ORDER BY pk`).
3. **Hash per segment.** On each side, one query:
   ```sql
   SELECT segment_id, COUNT(*), <aggregate_hash>(<row_hash>)
   FROM table WHERE pk >= lo AND pk < hi ... GROUP BY segment_id
   ```
   where `<row_hash>` is a hash of the *normalised* column values concatenated with a separator, and `<aggregate_hash>` is an order-independent aggregate (XOR or SUM of per-row hashes, mod 2^64, or engine-native such as ClickHouse `groupBitXor`). Each connector implements both expressions for its engine.
4. **Compare.** Segments with equal count and hash are done. Mismatched segments are bisected recursively (step 3 on the halves) until a segment is below `--row-threshold` (default 1000 rows).
5. **Row-level.** For each small mismatched segment, fetch `(pk, normalised columns)` from both sides, ordered by pk, and compute the exact set of missing / extra / changed rows in the client.
6. **Cap.** Stop collecting row-level differences after `--max-diff-rows` (default 10,000) and mark the result truncated. Counts stay exact; row lists don't.

Cost: for a 1-billion-row table with 100 scattered differences, this is on the order of a few hundred narrow queries per side, each touching one segment. That is the whole point.

### 4.2 Joindiff (same engine, same database)

When both tables live in the same database, a single query does the job:

```sql
SELECT ... FROM a FULL OUTER JOIN b ON a.pk = b.pk
WHERE a.pk IS NULL OR b.pk IS NULL OR <any normalised column differs>
```

Faster, exact, full row detail. Use automatically when both sides resolve to the same connection; force with `--algorithm joindiff`.

### 4.3 Sampling

`--sample 1%` (or `--sample-rows 1000000`): apply the same sampling predicate to both sides (deterministic, based on `hash(pk) % 100 < 1` so both sides pick the *same* rows), then run hashdiff on the sample. Report results as "of the sampled rows" with the sample size stated. Never silently sample.

### 4.4 Tables without a primary key

v1: refuse, with a message explaining that the user can pass `--key col1,col2` to nominate a unique column set. If the nominated key is not unique on either side, stop and say so with an example duplicate.

---

## 5. Connector interface

```python
class Connector(Protocol):
    engine: str                                  # "postgres" | "clickhouse" | "snowflake"

    def connect(self, dsn: str) -> None: ...
    def close(self) -> None: ...

    def get_schema(self, table: TableRef) -> list[Column]:
        """Column name, native type, nullable, ordinal position."""

    def get_primary_key(self, table: TableRef) -> list[str] | None: ...

    def query(self, sql: str, params: dict) -> list[tuple]: ...

    # SQL fragment generators — return SQL text, never execute
    def normalise_expr(self, column: Column, rule: NormalisationRule) -> str:
        """SQL expression that renders this column as the canonical string in §6."""
    def row_hash_expr(self, exprs: list[str]) -> str:
        """SQL expression hashing the concatenation of normalised column expressions."""
    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        """Order-independent aggregate over row hashes within a GROUP BY."""
    def quote_identifier(self, name: str) -> str: ...
    def quote_literal(self, value: object) -> str: ...
```

Rules for connectors:
- One connection per side. No connection pooling in v1.
- Every generated SQL statement is logged at `--verbose` so users can paste it into their own client when they don't believe the result. **This is a feature.** Users trust what they can reproduce.
- Row hash must be a **64-bit integer** on every engine so aggregates are comparable across engines: Postgres via `('x' || substr(md5(...), 1, 16))::bit(64)::bigint`, ClickHouse via `sipHash64` is **not** acceptable because it differs from Postgres — use `reinterpretAsInt64(unhex(substr(lower(hex(MD5(...))), 1, 16)))` or equivalent so **both sides compute the same MD5-derived 64-bit value from the same canonical string**. Whatever the mechanism, add a cross-engine test that hashes the same fixture string on every engine and asserts equality. That test is non-negotiable.

---

## 6. Normalisation rules — the product's real substance

Two values are compared by rendering each into a **canonical string** in SQL on its own engine, then hashing. The canonical forms below are the contract. Every rule has an ID; the row-level output cites the ID when a difference falls under it.

### 6.1 Canonical forms

| Rule | Type family | Canonical string | Notes |
|---|---|---|---|
| NULL-1 | any nullable | the literal `\N` | Distinct from empty string. `NULL` ≠ `''` always. |
| INT-1 | integers | decimal digits, no leading zeros, `-` sign | |
| DEC-1 | fixed-point decimal | digits with exactly `p` decimal places, where `p` = **min** scale of the two columns; round half-even | Report when scales differ (see 6.2) |
| FLT-1 | float / double | scientific notation with 15 significant digits | Floats are compared at 15 sig. digits by default. `--float-precision N` overrides. |
| STR-1 | text | as-is, UTF-8 | No trimming, no case folding by default |
| STR-2 | text, opt-in | `--trim` strips trailing whitespace; `--case-insensitive` lower-cases | Both off by default; when on, cited in output |
| BOOL-1 | boolean | `true` / `false` | Integers 0/1 in one engine vs boolean in the other: map, cite rule |
| TS-1 | timestamp with tz | ISO 8601 in UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | Always 6 fractional digits |
| TS-2 | timestamp precision differs | truncate both to the **lower** precision | Cite rule; e.g. Postgres µs vs ClickHouse DateTime (seconds) |
| TS-3 | timestamp without tz | rendered as-is with `Z` suffix, **assumed UTC**, warn once per run | `--assume-tz Asia/Kolkata` overrides |
| DATE-1 | date | `YYYY-MM-DD` | |
| TIME-1 | time | `HH:MM:SS.ffffff` | |
| UUID-1 | uuid | lower-case, hyphenated | |
| BIN-1 | bytea / binary | lower-case hex | |
| JSON-1 | json / jsonb | **v1: compare as text after engine-side canonicalisation if available**, else as raw text and warn | Proper key-order-independent JSON compare is v1.1 |
| ARR-1 | arrays | `[` + elements in canonical form joined by `,` + `]` | Order-sensitive |
| ENUM-1 | enums | the label as text | |
| UNK-1 | anything unmapped | cast to text, warn once per column, cite rule | Never fail a run because of an unknown type |

### 6.2 Column matching across sides

- Match by **name**, case-insensitive, after `--column-map a:b` overrides.
- Columns present on one side only: report as schema difference, exclude from hash, do not fail.
- Type families differ (e.g. `text` vs `varchar(50)`): fine. Families incompatible (e.g. `int` vs `timestamp`): exclude column, report loudly.
- Decimal scale differs: use DEC-1 (min scale) and print one warning naming both scales.

### 6.3 Key normalisation

Keys go through the same rules. A Postgres `bigint` key against a ClickHouse `UInt64` key compares fine; a Postgres `uuid` against a Snowflake `VARCHAR` compares after UUID-1. Segment boundaries are computed in canonical form and translated back to native literals per engine by the connector.

### 6.4 Fixtures (`tests/fixtures/`)

Each fixture: a value, its native type per engine, and the expected canonical string. Minimum set before M1 is called done:

```
timestamps:  2024-03-01 10:00:00+05:30  → 2024-03-01T04:30:00.000000Z
             2024-03-01 10:00:00.123456 (naive) → 2024-03-01T10:00:00.123456Z + TS-3 warning
             '0001-01-01', '9999-12-31 23:59:59'
decimals:    12.50 (numeric(10,2)) vs 12.5 (Decimal(10,1)) → "12.5" + DEC-1 note
             -0.00 → "0.00"
floats:      0.1 + 0.2 ; 1e308 ; -0.0 ; NaN (both sides NaN → equal); Infinity
strings:     'abc ' vs 'abc' (differ by default, equal under --trim)
             'Straße' vs 'STRASSE' (differ even case-insensitive — document this)
             emoji, RTL text, NUL byte, 10,000-char string
nulls:       NULL vs '' ; NULL vs 0 ; NULL vs 'NULL'
booleans:    true vs 1 ; false vs 0 ; 't' vs true (Postgres text)
uuids:       upper vs lower case ; with vs without hyphens (Snowflake)
arrays:      {1,2,3} vs [1,2,3] ; empty array vs NULL
```

---

## 7. CLI

```
tablediff diff SOURCE TARGET [options]

  SOURCE / TARGET: connection-string + table, e.g.
    postgres://user:pw@host:5432/db/public.orders
    clickhouse://user:pw@host:8123/db/orders
    snowflake://user:pw@account/db/schema.orders
  or a named connection from the config file: prod_pg/public.orders

Options
  --key COLS              comma-separated key columns (default: detected PK)
  --columns COLS          only compare these
  --exclude COLS          skip these
  --where SQL             applied to both sides (or --where-source / --where-target)
  --algorithm auto|hashdiff|joindiff
  --sample PCT | --sample-rows N
  --threads N             parallel segment queries per side (default 4)
  --row-threshold N       bisect until segment ≤ N rows (default 1000)
  --max-diff-rows N       cap on collected row differences (default 10000)
  --trim / --case-insensitive / --float-precision N / --assume-tz TZ / --column-map a:b,...
  --output terminal|json|html   (repeatable; --html-path, --json-path)
  --verbose               log every generated SQL statement
  --fail-on none|any|count   (exit-code policy, default any)

tablediff run CONFIG.yaml          run many table pairs; one report
tablediff connections test NAME    check credentials
tablediff explain SOURCE TARGET    print the SQL it would run, run nothing
```

Exit codes: `0` tables match · `1` differences found · `2` could not compare (schema incompatible, missing key, connection failure). CI pipelines depend on this; document it on the README front page.

Secrets: accept `${ENV_VAR}` in config and DSNs. Never print passwords, even at `--verbose`; redact before logging.

---

## 8. Output

### 8.1 Terminal (rich)

```
orders  postgres:prod/public.orders  →  clickhouse:analytics/orders
  rows           12,481,902           12,481,877        ▲ 25 missing in target
  key            order_id (bigint → UInt64)
  columns        14 compared, 1 excluded (raw_payload: json vs String, JSON-1)
  algorithm      hashdiff · 128 segments · 41 queries/side · 18.4s

  missing in target   25
  extra in target      0
  changed              3
    order_id=4471102  updated_at  2024-03-01T10:00:00.123456Z → 2024-03-01T10:00:00.000000Z  (TS-2: target precision is seconds)
    order_id=4471299  total       "129.99" → "130.00"
    order_id=4480001  status      "shipped" → "SHIPPED"   (use --case-insensitive to ignore)

  result   DIFFERENT   exit 1
```

### 8.2 JSON

Stable schema, versioned (`"schema_version": 1`). Contains everything in the terminal output plus every generated SQL statement (redacted), timings, and the warnings list. This is what the paid tier and CI consume.

### 8.3 HTML sign-off report

One self-contained file. Sections: summary banner (MATCH / DIFFERENT / INCOMPLETE), per-table cards, sample of differing rows (capped, with rule citations), warnings, run metadata (who, when, versions, both DSNs with secrets redacted), and a **"Reproduce"** section with the SQL used. Print-friendly. This is the artefact a data lead attaches to a cutover approval; it must look serious. No external fonts, no JavaScript required to read it.

---

## 9. Milestones

Each ends with a tagged release installable via `pipx`.

**M0 — Postgres ↔ Postgres hashdiff (week 1–2)**
Core algorithm with fake connectors and full unit tests; Postgres connector; terminal output; `explain` command. No normalisation beyond NULL-1, INT-1, STR-1, TS-1.

**M1 — Normalisation and joindiff (week 2–3)**
All §6 rules for Postgres; fixture suite; joindiff; JSON output; config file and `run`.

**M2 — ClickHouse (week 3–4)**
Connector; cross-engine hash-equality test; segment translation for UInt keys; `DateTime` vs `DateTime64` precision (TS-2); `Nullable()` handling; `LowCardinality`, `Enum8/16`, `Array`, `Map` (as text, UNK-1) types.

**M3 — Snowflake and HTML report (week 4–5)**
Connector as optional extra; `NUMBER(38,0)` as integer; `VARIANT` as text; `TIMESTAMP_NTZ/LTZ/TZ` rules; HTML sign-off report; sampling.

**M4 — Launch hardening (week 5–6)**
README with a 60-second demo GIF; docs site (mkdocs, single page is fine); `--threads`; connection retries; clear errors for every failure in §10; PyPI release `0.1.0`; launch posts.

**M5 — Post-launch (ongoing)**
Fix whatever the first hundred users hit. Databricks and BigQuery connectors next, in whichever order the issues demand.

---

## 10. Non-negotiable quality bar

| Requirement | Why |
|---|---|
| Never report MATCH when rows differ | The only unforgivable bug |
| Cross-engine hash equality test passes on every supported engine | Otherwise hashdiff is meaningless |
| 100M-row Postgres ↔ ClickHouse diff with 1,000 differences completes in under 5 minutes on modest hardware | Otherwise users go back to pandas |
| Client memory stays under 500 MB regardless of table size | Nothing full-table ever reaches the client |
| Every warning names the rule ID and the column | Users must be able to reason about results |
| `--verbose` shows reproducible SQL | Trust |
| Clean error for: bad credentials, missing table, no key, non-unique key, incompatible types, network drop mid-run | Every one of these will happen in the first week |
| Works with read-only database roles | Many users only have read access to prod |
| Zero telemetry | Data engineers will check. Say so in the README. |

---

## 11. Definition of done for v1 (`0.1.0`)

- All M0–M3 acceptance criteria (§13) pass
- Fixture suite passes on Postgres and ClickHouse in CI via testcontainers; Snowflake fixtures pass locally with credentials
- 100M-row benchmark recorded in `docs/benchmarks.md` with hardware stated
- README: install, one-command demo, exit codes, supported types table, "how it works" diagram, "we never write to your database" statement
- `tablediff explain` output pasted into README as proof of transparency
- PyPI package installs cleanly on Linux and macOS, Python 3.11–3.13
- Launch posts drafted for Hacker News (Show HN), r/dataengineering, dbt Slack, LinkedIn

---

## 12. Paid tier — for context only, do not build in v1

Separate closed repo. Scheduled runs, run history, Slack/email alerts, multi-table migration dashboards, sign-off reports with approval workflow, team access. Consumes the JSON output (§8.2) — which is why that schema must be stable and versioned from day one. Pricing target ~$150/month per workspace, plus a flat per-migration tier for consultancies.

---

## 13. Acceptance criteria

Verify on real databases before calling a milestone done.

### M0
- [ ] Two identical 1M-row Postgres tables → MATCH, exit 0, under 10s
- [ ] Delete 10 rows, update 10 rows, insert 10 rows in the target → exactly those 30 reported, correctly classified
- [ ] Composite key (`tenant_id, order_id`) works
- [ ] UUID key segments correctly (no segment holds >2× the average rows)
- [ ] Table without PK and no `--key` → exit 2 with a clear message
- [ ] Non-unique `--key` → exit 2, shows one duplicate example
- [ ] `explain` prints SQL and executes nothing (verify via query log)
- [ ] Kill the network mid-run → clean error, exit 2, no traceback
- [ ] Password never appears in any output, including `--verbose`

### M1
- [ ] Every fixture in §6.4 produces the expected canonical string on Postgres
- [ ] `NULL` vs `''` reported as different; `--trim` and `--case-insensitive` behave and are cited
- [ ] Naive timestamps produce exactly one TS-3 warning per run
- [ ] Joindiff and hashdiff give identical results on the same table pair
- [ ] `run config.yaml` with 20 table pairs produces one combined result and correct exit code
- [ ] JSON output validates against the published schema

### M2
- [ ] Cross-engine hash equality test passes for every fixture string
- [ ] Postgres `timestamptz(6)` vs ClickHouse `DateTime` → differences within the same second are **not** reported; a TS-2 note appears once
- [ ] Postgres `numeric(12,2)` vs ClickHouse `Decimal(12,4)` → compared at scale 2, DEC-1 note
- [ ] ClickHouse `Nullable(String)` NULL vs Postgres NULL → equal
- [ ] 100M-row benchmark under 5 minutes, memory under 500 MB

### M3
- [ ] Snowflake `NUMBER(38,0)` vs Postgres `bigint` → equal for the same values
- [ ] `TIMESTAMP_NTZ` handled with TS-3 warning; `TIMESTAMP_TZ` converts to UTC
- [ ] HTML report opens from a local file with no network and prints to A4 cleanly
- [ ] `--sample 1%` selects the same rows on both sides (verify by diffing a sample against itself → MATCH)
- [ ] `pipx install tablediff[snowflake]` works on a clean machine

### M4
- [ ] Fresh user follows README and gets a diff result in under 5 minutes
- [ ] All error paths in §10 produce a one-line human message, with traceback only under `--verbose`
- [ ] `0.1.0` on PyPI; GitHub release notes; demo GIF in README

---

## 14. Launch checklist (week 6)

- Show HN title: "Show HN: TableDiff – verify tables match across Postgres, ClickHouse and Snowflake" — lead with the problem, mention data-diff's sunset in the first comment, not the title
- r/dataengineering: post as a question-and-answer ("we kept getting bitten by timestamp precision during a Postgres→ClickHouse migration, so I built…")
- dbt Community Slack `#tools-*` and `#advice-*` channels; Data Engineering Discord
- LinkedIn: one post, the demo GIF, three sentences
- Comment on the archived `datafold/data-diff` issues that asked for a maintained alternative — helpfully, with a link, once
- Reply to every comment within 24 hours for the first two weeks; each reply is discovery
- Track: GitHub stars, PyPI downloads, issues opened, and which engines people ask for next. That order decides M5.
