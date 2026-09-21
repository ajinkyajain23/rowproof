# rowproof

**rowproof proves that a database migration or replication didn't lose, add or change a single row — and gives you a report you can sign off on.**

Diffing is the plumbing. The product is the proof: exact counts, every difference explained with a rule citation, reproducible SQL, and a self-contained HTML sign-off report.

![rowproof HTML sign-off report](docs/screenshot-report.png)

_60-second terminal demo GIF: recording script is in [docs/demo.tape](docs/demo.tape) (`vhs docs/demo.tape`, needs `ttyd`+`ffmpeg`) — not yet rendered in this environment; see the CLI output further down for what it shows._

## Install

```bash
pipx install rowproof
pipx install "rowproof[snowflake]"   # adds Snowflake support
```

(`pip install rowproof` / `pip install "rowproof[snowflake]"` work too — `pipx` just keeps the CLI's dependencies out of your other Python environments.)

## 60-second demo

Using the Postgres and ClickHouse containers from this repo's `docker-compose.yml`:

```bash
docker compose up -d
rowproof diff postgres://postgres:postgres@localhost:5432/postgres/public.orders \
              clickhouse://default:clickhouse@localhost:8123/default/orders \
              --key id --output html --html-path report.html
```

Two commands. Open `report.html` and you have a sign-off document — exact row counts on both sides, every difference cited against a published rule, and the SQL to reproduce each one.

Run it with the default terminal output instead (no `--output`/`--html-path`) and here's what a real run looks like — this table pair has one row deliberately changed and one deliberately deleted on the ClickHouse side, and rowproof catches both:

```
$ rowproof diff postgres://postgres:postgres@127.0.0.1:5432/postgres/public.orders \
               clickhouse://default:clickhouse@127.0.0.1:8123/default/orders --key id
public.orders  postgres:postgres/public.orders  ->  clickhouse:default/orders
  rows           5000                 4999
  key            id
  algorithm      hashdiff · 8 segments · 10 queries/side · 0.2s

  missing in target   1
  extra in target     0
  changed             1

    key=42  customer  'customer-42' -> 'customer-999'  (STR-1)
    key=4500  missing
  warning: row counts differ: source=5000 target=4999

  result   DIFFERENT   exit 1
```

**Next:** the [user guide](docs/USAGE.md) covers connection strings for each database, every option with examples, checking many tables from a config file, using rowproof in CI, and troubleshooting.

## What it checks — and what it never does

- Verifies every row matches: counts, missing rows, extra rows, and changed values, down to the specific column and the exact rule that governed the comparison (a timestamp precision mismatch and a NULL-vs-empty-string mismatch are not the same bug, and rowproof tells you which one you have).
- **Never writes to your database.** Every connector is read-only by construction — `SELECT` and schema-introspection queries only, nothing else. It works with a read-only database role.
- **Zero telemetry.** Nothing phones home. Read the source; there's no network call in this codebase that isn't the one you told it to make.

## Supported engines

| Engine | Driver | Notes |
|---|---|---|
| Postgres | `psycopg` (v3) | |
| ClickHouse | `clickhouse-connect` | |
| Snowflake | `snowflake-connector-python` | Optional extra: `rowproof[snowflake]` |

### Snowflake: known limitations

Snowflake support works, but it is the least battle-tested of the three engines. It has been tested against a single trial account with small tables; Postgres and ClickHouse have had far more. Know these before you rely on it:

- **Login is username and password only.** The connection string has no place for a warehouse, role, SSO, MFA, or key-pair login. Accounts that require SSO or key-pair authentication won't work yet, and queries run on the user's *default* warehouse (a user with none set will fail).
- **Column names are case-sensitive.** Snowflake stores unquoted names in upper case, so `--key id` won't match a column stored as `ID`. Pass `--key ID`, or create the table with quoted lower-case names. The error message tells you when this is the problem.
- **Primary keys are often not declared** on Snowflake tables (Snowflake doesn't enforce them). If auto-detection finds none, pass `--key`.
- **JSON columns are compared as plain text.** `VARIANT`, `ARRAY` and `OBJECT` use rule UNK-1: two JSON values that mean the same thing but are formatted differently will show as different, and a JSON `null` is not distinguished from an SQL `NULL`.
- **Special characters in passwords must be percent-encoded** in the connection string (`@` becomes `%40`).
- **It uses your warehouse's credits.** Every query runs on your Snowflake warehouse. Start with a small table or `--sample`.
- **No Snowflake benchmark yet.** The 100M-row benchmark below is Postgres to ClickHouse only.

## Supported types (canonical comparison rules)

Every cross-engine type difference is governed by one of these published rules, and every difference rowproof reports cites the rule ID that applied — never a vague "values differ."

| Rule | Applies to | Canonical form | Notes |
|---|---|---|---|
| NULL-1 | any nullable | the literal `\N` | Distinct from empty string. `NULL` ≠ `''` always. |
| INT-1 | integers | decimal digits, no leading zeros, `-` sign | |
| DEC-1 | fixed-point decimal | digits at the pair's minimum scale, round half-even | Reported when scales differ |
| FLT-1 | float / double | scientific notation, 15 significant digits by default | `--float-precision N` overrides |
| STR-1 | text | as-is, UTF-8 | No trimming, no case folding by default |
| STR-2 | text, opt-in | `--trim` / `--case-insensitive` | Cited in output when active |
| BOOL-1 | boolean | `true` / `false` | Handles integer 0/1 vs boolean across engines |
| TS-1 | timestamp with tz | ISO 8601 UTC, `YYYY-MM-DDTHH:MM:SS.ffffffZ` | Always 6 fractional digits |
| TS-2 | timestamp precision differs | rounded to the pair's lower precision | e.g. Postgres µs vs ClickHouse `DateTime` (seconds) |
| TS-3 | timestamp without tz | rendered as-is with `Z`, assumed UTC, warns once | Always UTC for now; `--assume-tz` other than UTC is rejected (not implemented yet) |
| DATE-1 | date | `YYYY-MM-DD` | |
| TIME-1 | time | `HH:MM:SS.ffffff` | |
| UUID-1 | uuid | lower-case, hyphenated | Handles a uuid stored as VARCHAR (Snowflake) |
| BIN-1 | bytea / binary | lower-case hex | |
| JSON-1 | json / jsonb | text after engine-side canonicalisation if available | Full key-order-independent compare is a future release |
| ARR-1 | arrays | `[` + elements joined by `,` + `]` | Order-sensitive |
| ENUM-1 | enums | the label as text | |
| UNK-1 | anything unmapped | cast to text, warns once, cites the rule | Never fails a run over an unknown type |

## How it works

1. Get `MIN(pk)`, `MAX(pk)`, `COUNT(*)` on both sides.
2. Split the key range into segments (~100k rows each).
3. Hash each segment's normalised rows into one aggregate per side, in one query — the databases do the work, not the client.
4. Compare hashes. Matching segments are done; mismatched ones bisect recursively until small.
5. For small mismatched segments, fetch normalised rows from both sides and diff them in the client.
6. Cap the collected row-level differences (`--max-diff-rows`); counts stay exact regardless.
7. When both tables live in the same connection, skip all of the above — one `FULL OUTER JOIN` does it exactly, in one query.
8. Client memory never holds more than one segment's worth of rows at a time — a 100-million-row table and a 100-row table cost the client about the same.

## `rowproof explain` — proof of transparency

`explain` prints the exact SQL a `diff` would run, without ever executing it against your data:

```
$ rowproof explain postgres://postgres:postgres@127.0.0.1:5432/postgres/public.orders \
                   clickhouse://default:clickhouse@127.0.0.1:8123/default/orders --key id
-- source: row-count and bounds
SELECT MIN("id"), MAX("id"), COUNT(*) FROM "public"."orders"
-- target: row-count and bounds
SELECT MIN(`id`), MAX(`id`), COUNT(*) FROM `orders`
-- source: uniqueness check on the key
SELECT "id", COUNT(*) FROM "public"."orders" GROUP BY "id" HAVING COUNT(*) > 1 LIMIT 1
-- target: uniqueness check on the key
SELECT `id`, COUNT(*) FROM `orders` GROUP BY `id` HAVING COUNT(*) > 1 LIMIT 1
-- source: per-segment count + hash (one per segment)
SELECT COUNT(*), ((sum(((('x' || substr(md5(("id"::text) || E'\x1f' || ...
-- target: per-segment count + hash (one per segment)
SELECT COUNT(*), ((sum(toInt256(reinterpretAsInt64(reverse(unhex(substr(lower(hex(MD5(...
```

Every line above is real output from an actual Postgres/ClickHouse pair — copied verbatim, not hand-written. The hash expressions get long fast (each engine's DEC-1/TS-1 rendering is spelled out in full, no black box); the complete, unedited transcript for this exact table pair is in [docs/explain-output.txt](docs/explain-output.txt).

## Exit codes (for CI)

| Code | Meaning |
|---|---|
| `0` | Tables match |
| `1` | Differences found |
| `2` | Could not compare (schema incompatible, missing key, connection failure) |

## How is this different from reladiff / data-diff?

[reladiff](https://github.com/erezsh/reladiff) is an excellent, actively maintained diffing library, and rowproof uses the same hash-and-bisect idea it and its predecessor `data-diff` popularized. rowproof differs in three ways: every cross-engine type difference is governed by a published rule table and cited in the output; the output is a sign-off report meant to be attached to a cutover approval, not just a diff count; and the CLI is the free core of a hosted reconciliation service (scheduled runs, history, alerts) rather than a standalone library.

## Benchmark

100M rows, verified correct (exactly the 1,000 differences introduced, nothing else), 127 MB peak client memory, 8.95 minutes on a laptop with both databases sharing the same 8 vCPUs. Full methodology and hardware details in [docs/benchmarks.md](docs/benchmarks.md).

## Roadmap

- More engines: Databricks, BigQuery, Iceberg, MySQL
- A hosted reconciliation service: scheduled runs, run history, Slack/email alerts, sign-off reports for a whole migration. Waitlist link coming soon.

## Licence

Apache 2.0.
