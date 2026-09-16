# r/dataengineering draft

**Suggested title:** We kept getting bitten by timestamp precision and NULL-vs-empty during a Postgres→ClickHouse migration, so I built a row-level diff tool

**Body:**

We kept getting bitten by timestamp precision and NULL-vs-empty during a Postgres→ClickHouse migration, so I built a tool to catch it before it bites in production instead of after.

The pattern that kept happening: row counts matched, some spot-checks passed, we called the migration done — and then weeks later someone would notice a report was off by a handful of rows, and it would take an afternoon to trace it back to a `timestamptz` in Postgres losing precision against ClickHouse's `DateTime`, or a `NULL` in one system becoming an empty string in the other. Neither of those shows up in a `COUNT(*)` comparison. Both are silent data corruption if nobody catches them.

So I built [rowproof](#) — it diffs two tables across Postgres, ClickHouse, and Snowflake, hash-and-bisects so it never has to pull a full table to the client (segments the key range, hashes each segment in one query per side, only fetches actual row detail for segments that disagree), and — the part that actually mattered for us — every difference it reports cites a specific rule: "this is DEC-1, the two sides declared different decimal scales" or "this is TS-3, one side's timestamp has no timezone and got assumed UTC," not just "these two values are different." It also writes a self-contained HTML report so the diff results are something you can actually hand to someone for a migration sign-off, not just a CI log.

It's free and open source (Apache 2.0). Point at your own Postgres/ClickHouse and it should run in a couple of minutes: [link].

Anyone else have a favorite "row counts matched but the migration was still wrong" war story? Curious what other silent-corruption patterns are common enough to be worth a named rule for.
