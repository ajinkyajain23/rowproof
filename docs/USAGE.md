# rowproof user guide

How to use rowproof, task by task. Every command below was run against real Postgres, ClickHouse and Snowflake instances, and the output shown is real.

- [The basics](#the-basics)
- [Connection strings](#connection-strings)
- [Choosing the key](#choosing-the-key)
- [Reading the output](#reading-the-output)
- [Common tasks](#common-tasks)
- [Reports: HTML and JSON](#reports-html-and-json)
- [Checking many tables at once](#checking-many-tables-at-once)
- [Using rowproof in CI](#using-rowproof-in-ci)
- [Snowflake](#snowflake)
- [Troubleshooting](#troubleshooting)
- [Known limitations](#known-limitations)

---

## The basics

```
rowproof diff SOURCE TARGET --key COLUMN
```

`SOURCE` and `TARGET` are each a connection string plus a table. `--key` names the column (or columns) that uniquely identify a row.

```
$ rowproof diff postgres://postgres:postgres@127.0.0.1:5432/postgres/public.orders \
                clickhouse://default:clickhouse@127.0.0.1:8123/default/orders --key id
public.orders  postgres:postgres/public.orders  ->  clickhouse:default/orders
  rows           5000                 5000
  key            id
  algorithm      hashdiff · 8 segments · 8 queries/side · 0.2s

  missing in target   0
  extra in target     0
  changed             0

  result   MATCH   exit 0
```

Other commands:

| Command | What it does |
|---|---|
| `rowproof diff SOURCE TARGET` | Compare two tables |
| `rowproof explain SOURCE TARGET` | Print the SQL a diff would run, without running it against your data |
| `rowproof run CONFIG.yaml` | Compare many table pairs from a config file |
| `rowproof connections test NAME` | Check that a named connection from a config file can log in |
| `rowproof --version` | Print the version |

Run any command with `--help` to see every option.

**rowproof never writes to your database.** It only runs `SELECT` and schema-lookup queries, so a read-only database user is enough (verified with a `SELECT`-only Postgres user).

---

## Connection strings

```
engine://user:password@host:port/database/schema.table
```

| Engine | Format | Notes |
|---|---|---|
| Postgres | `postgres://user:pw@host:5432/mydb/public.orders` | The schema defaults to `public` if you leave it out: `.../mydb/orders` |
| ClickHouse | `clickhouse://user:pw@host:8123/mydb/orders` | Port `8123` is ClickHouse's HTTP port. The database comes from the URL; there is no separate schema |
| Snowflake | `snowflake://user:pw@ACCOUNT/MYDB/PUBLIC.ORDERS` | `ACCOUNT` is your account identifier, for example `abc12345.ap-south-1.aws`. See [Snowflake](#snowflake) |

**Special characters in a password** must be percent-encoded, or the string will be misread. `@` becomes `%40`, `:` becomes `%3A`, `/` becomes `%2F`. A password of `p@ss` is written `p%40ss`.

**Keeping the password out of your shell history:** put the connection strings in a [config file](#checking-many-tables-at-once) and reference environment variables with `${NAME}`. rowproof never prints passwords; the HTML report shows connection strings with the password replaced by `***`.

Two tables in the *same* database (same connection string) are compared with a single exact SQL join, which is faster. Tables in different databases use the hash-and-bisect method. rowproof picks automatically; see [algorithms](#speed-and-large-tables).

---

## Choosing the key

The key is the column(s) that identify a row: usually the primary key. rowproof needs it to line rows up between the two tables.

- **Leave `--key` off** and rowproof uses the table's declared primary key. This worked on Postgres, ClickHouse and Snowflake.
- **Give `--key`** when there is no declared primary key, or you want a different column: `--key id`.
- **Composite keys:** `--key tenant_id,order_id`.

The key must be unique on both sides. If it isn't, rowproof stops and shows a duplicate:

```
$ rowproof diff .../public.dup_a .../public.dup_a --key id
error: --key id is not unique on 'public.dup_a': found a duplicate at id=1.
[exit 2]
```

If there's no primary key and you didn't pass `--key`:

```
error: Table 'public.nopk_a' has no primary key and no --key was given. Pass --key col1,col2 to nominate a unique column set.
[exit 2]
```

---

## Reading the output

```
  rows           5000                 4999            <- row count: source, target
  missing in target   1                                <- in source, not in target
  extra in target     0                                <- in target, not in source
  changed             1                                <- in both, but a value differs

    key=42  customer  'customer-42' -> 'customer-999'  (STR-1)
    key=4500  missing
```

- Each changed value shows the **column**, the **source value → target value**, and the **rule** (`STR-1`, `DEC-1`, `TS-1`, ...) that decided how the two were compared. The rules are listed in the [README](../README.md#supported-types-canonical-comparison-rules).
- `warning:` lines are things worth knowing that didn't stop the run: mismatched decimal scales, timestamps with no timezone, columns present on only one side, and so on. Warnings about how values were compared name the rule and the column.
- Long lists are capped (`--max-diff-rows`, default 10,000). The counts stay exact even when the list is cut short.
- **`MATCH` vs `MATCH (PARTIAL: N columns NOT compared)`.** A plain `MATCH` means every column was compared. If some columns were left out because their types can't be compared (a type changed in the migration, or a column exists on only one side), the verdict says so and names how many; the `columns` line and the warnings name which. The exit code is still `0`, but the tables are only proven equal for the columns that were compared. The HTML report shows an amber banner instead of a green one, and the JSON has `"fully_compared": false`.

**Exit codes:**

| Code | Meaning |
|---|---|
| `0` | The tables match |
| `1` | Differences were found |
| `2` | rowproof could not compare (bad login, missing table, no key, ...) |

---

## Common tasks

### Compare only some rows

`--where` applies a SQL filter to both sides. It's plain SQL in each database's own dialect, so keep it simple enough to be valid in both.

```
$ rowproof diff .../public.orders .../public.orders_v2 --key id --where "id > 15"
  rows           4985                 4985
  changed             2
```

If the two databases need different filters, use `--where-source` and `--where-target` (each overrides `--where` for its side).

### Compare only some columns, or skip some

```
rowproof diff SOURCE TARGET --key id --columns amount,customer
rowproof diff SOURCE TARGET --key id --exclude created_at
```

### Columns with different names

`--column-map source_name:target_name`, comma-separated for several:

```
rowproof diff SOURCE TARGET --key id --column-map customer:cust,total:amount
```

### Check a sample instead of everything

For a quick check on a huge table. The same rows are picked on both sides, and the result is clearly labelled as a sample.

```
$ rowproof diff SOURCE TARGET --key id --sample 10%
  SAMPLED        10% of rows · counts below are of the sample only
  rows           515                  515
```

`--sample-rows 100000` targets roughly that many rows instead. Sampling only works between two different databases (or with `--algorithm hashdiff`).

### Ignore trailing spaces or letter case

Both are off by default, because either can be a real difference:

```
rowproof diff SOURCE TARGET --key id --trim              # ignore trailing whitespace
rowproof diff SOURCE TARGET --key id --case-insensitive  # ignore upper/lower case
```

When on, the comparison rule is cited as `STR-2`.

### Compare floats less strictly

Floats are compared to 15 significant digits. Use `--float-precision 10` to compare fewer.

### Speed and large tables

| Option | Default | What it does |
|---|---|---|
| `--algorithm auto\|hashdiff\|joindiff` | `auto` | `auto` uses `joindiff` (one SQL join) when both tables are on the same connection, otherwise `hashdiff`. `joindiff` only works within one database |
| `--threads N` | 4 | Parallel queries per side (`hashdiff` only) |
| `--row-threshold N` | 1000 | When a mismatched chunk is smaller than this, fetch its rows and compare directly |
| `--max-diff-rows N` | 10000 | Stop listing differences after this many (counts stay exact) |

On the author's laptop, with both databases on the same 8 CPU cores, 100 million rows compared correctly in 8.95 minutes using 127 MB of client memory. See [benchmarks](benchmarks.md).

### See exactly what it's doing

```
rowproof explain SOURCE TARGET --key id      # print the SQL, run no data queries
rowproof diff SOURCE TARGET --key id --verbose   # print every query as it runs
```

`explain` reads only the table structure from each database. The per-chunk queries it prints show `'<segment lower bound>'` placeholders, because the real boundaries depend on your data.

---

## Reports: HTML and JSON

`--output` chooses the format, and you can repeat it:

```
rowproof diff SOURCE TARGET --key id --output html --html-path report.html
rowproof diff SOURCE TARGET --key id --output json --json-path result.json
rowproof diff SOURCE TARGET --key id --output terminal --output html --html-path report.html
```

- **HTML** is a single self-contained file (no internet needed) meant to be attached to a sign-off: a MATCH/DIFFERENT banner, counts, the differing rows with rule citations, warnings, who ran it and when, the redacted connection strings, and the exact SQL that was run.
- **JSON** goes to your terminal unless you give `--json-path`. It carries `schema_version` (currently `1`) and `"tool": "rowproof"`, so other programs can rely on its shape. Fields: `is_match`, `exit_code`, `source_count`, `target_count`, `missing_in_target`, `extra_in_target`, `changed`, `row_diffs`, `warnings`, `sql_statements`, `timings`, and more.
- If you ask for `html` or `json` only, the terminal summary is not printed. Add `--output terminal` as well to get both.

---

## Checking many tables at once

Put connections and table pairs in a YAML file:

```yaml
connections:
  prod_pg: postgres://postgres:${PG_PASSWORD}@127.0.0.1:5432/postgres
  analytics_ch: clickhouse://default:${CH_PASSWORD}@127.0.0.1:8123/default

tables:
  - source: prod_pg/public.orders
    target: analytics_ch/orders
    key: id
  - source: prod_pg/public.orders
    target: analytics_ch/orders
    key: id
    where: id <= 100
    fail_on: count
```

```
export PG_PASSWORD=...  CH_PASSWORD=...        # bash
$env:PG_PASSWORD="..."; $env:CH_PASSWORD="..."  # PowerShell

rowproof connections test prod_pg --config rowproof.yaml     # check the login first
rowproof run rowproof.yaml
```

- `${NAME}` is replaced with the environment variable of that name, anywhere in the file. If the variable isn't set, rowproof stops with a clear message rather than using an empty password.
- A table pair is written `connection_name/schema.table`.
- Each table accepts the same options as `diff`: `key`, `columns`, `exclude`, `where`, `where_source`, `where_target`, `algorithm`, `row_threshold`, `max_diff_rows`, `trim`, `case_insensitive`, `float_precision`, `column_map`, `fail_on`. `key`, `columns` and `exclude` can be a list or a comma-separated string.
- The results print one after another. The final exit code is the worst one: any `2` beats any `1` beats `0`.
- `run` prints terminal summaries only. It does not take `--output`, `--sample` or `--threads`.
- Named connections work in the config file and in `run`, but not directly in `rowproof diff`.

---

## Using rowproof in CI

The exit code is the contract. A pipeline step that fails on any difference needs nothing more than the command:

```bash
pip install rowproof
rowproof diff "$SOURCE_DSN/public.orders" "$TARGET_DSN/orders" --key id \
  --output html --html-path report.html
```

`--fail-on` changes when the exit code is non-zero:

| `--fail-on` | Exit code is 1 when... |
|---|---|
| `any` (default) | any difference is found |
| `count` | only the row counts differ |
| `none` | never (still `2` if it could not compare) |

Note: with `--fail-on count` or `none`, the summary's last line still reads `result DIFFERENT exit 1`, but the process exit code follows `--fail-on`. Trust the process exit code.

A GitHub Actions job that keeps the report as a downloadable artifact (adapt the secrets and paths):

```yaml
jobs:
  verify-migration:
    runs-on: ubuntu-latest
    steps:
      - run: pip install rowproof
      - run: >
          rowproof diff "${{ secrets.SOURCE_DSN }}/public.orders"
          "${{ secrets.TARGET_DSN }}/orders" --key id
          --output html --html-path report.html
      - uses: actions/upload-artifact@v4
        if: always()
        with:
          name: rowproof-report
          path: report.html
```

---

## Snowflake

Install the extra: `pip install "rowproof[snowflake]"`.

```
snowflake://USER:PASSWORD@ACCOUNT/DATABASE/SCHEMA.TABLE
```

Real run, comparing two Snowflake tables (one value changed in the second):

```
$ rowproof diff snowflake://USER:PASSWORD@ACCOUNT/DATABASE/PUBLIC.ORDERS_A \
                snowflake://USER:PASSWORD@ACCOUNT/DATABASE/PUBLIC.ORDERS_B
  rows           3                    3
  key            ID
  algorithm      joindiff · 1 segments · 1 queries/side · 2.7s

  changed             1

    key=2  NAME  'b' -> 'CHANGED'  (STR-1)

  result   DIFFERENT   exit 1
```

**Column names are case-sensitive.** Snowflake stores unquoted names in upper case, so a column you created as `id` is really `ID`:

```
$ rowproof diff ... --key id
error: Key column 'id' not found in 'PUBLIC.ORDERS_A'. Did you mean 'ID'? Column names are case-sensitive here (Snowflake stores unquoted names in upper case). Available columns: ID, NAME, AMOUNT.
```

Pass `--key ID`, or leave `--key` off if the table declares a primary key.

Everything else that limits Snowflake support (login methods, JSON columns, credits) is listed in the [README](../README.md#snowflake-known-limitations).

---

## Troubleshooting

| You see | It means | Do this |
|---|---|---|
| `error: connection timeout expired` | rowproof can't reach the database at all | Check the host and port, and that the database is running. If you use Docker, check the containers are up |
| `error: connection failed: ... password authentication failed` | Wrong username or password | Check both. Special characters in the password must be [percent-encoded](#connection-strings) |
| `error: Table '...' not found, or it has no visible columns` | The table doesn't exist, or this login can't see it | Check the name and schema, and that the user has read access |
| `error: Key column '...' not found in '...'` | `--key` names a column that isn't there | The message lists the real columns. For Snowflake, match the upper-case spelling |
| `error: Table '...' has no primary key and no --key was given` | Nothing to identify rows by | Add `--key` |
| `error: --key ... is not unique` | Two rows share that key value | Use a truly unique column, or a composite key |
| `MATCH (PARTIAL: N columns NOT compared)` | Some columns have types that can't be compared with each other | Read the `warning: column '...' excluded` lines. Fix the type on one side if the change wasn't intended |
| `warning: TS-3: naive timestamp column(s) compared as UTC` | A timestamp column has no timezone | rowproof compares it as UTC. Fine if that's true; otherwise use timezone-aware columns |
| `warning: DEC-1 ... scale differs` | The two decimal columns have different decimal places | rowproof compares at the smaller scale; nothing to fix unless it's unexpected |
| `error: --assume-tz ... is not supported yet` | See [limitations](#known-limitations) | Remove the flag |
| Same rows show as different by a hair | Float or timestamp precision differs between engines | Read the rule ID in the output (`FLT-1`, `TS-2`, ...) and lower `--float-precision` if appropriate |

Add `--verbose` to any command to also print every SQL statement, and a full traceback if something goes wrong. That is the most useful thing to include when reporting a problem.

---

## Known limitations

- **`--assume-tz` is not implemented.** Timestamps with no timezone are always compared as UTC. Passing any other zone is refused with an error rather than silently ignored. If your naive timestamps are really in another zone, convert them in a view or use timezone-aware columns.
- **Snowflake login is username and password only**: no SSO, MFA, key-pair, or warehouse/role options. See the [README](../README.md#snowflake-known-limitations).
- **`rowproof run` is terminal-only**: no HTML/JSON output, sampling or threads option per table yet.
- **Named connections** (`prod_pg/public.orders`) only work through a config file and `run`.
- **Some type pairs are never compared**, and show up as `NOT compared`: a `bytea`/binary column against a text column, and an integer against a text column. Booleans against integers (0/1), and enums against text, *are* compared.
- **JSON columns are compared as text** (rule `JSON-1` / `UNK-1`), so equivalent JSON formatted differently can show as different.
- Supported databases: Postgres, ClickHouse, Snowflake. Databricks, BigQuery, Iceberg and MySQL are on the roadmap.
