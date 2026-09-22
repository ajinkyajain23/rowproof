# r/dataengineering draft

**Suggested title:** Row counts matching doesn't mean your migration is correct — built a tool after proving it on a real database

**Body:**

"Row counts match" is not the same as "the migration is correct," and I wanted something that actually checks the second thing.

To test that claim rather than just assert it, I took a public sample database (Pagila, a DVD-rental schema — enums, arrays, decimals, timestamps with timezone, the usual mess) and migrated it from Postgres into ClickHouse. Row counts matched on all 15 tables. Then I ran the tool on the same tables. One table was actually wrong: a `character(20)` column had picked up trailing-space padding during the migration — invisible in a row count, invisible in a casual `SELECT *`. On three more tables, columns had silently been left out of the check entirely, because their types didn't line up between the two databases (a boolean stored as an integer on one side, an enum that became plain text on the other) — which I then fixed, so those get compared too instead of quietly skipped.

So: [rowproof](https://github.com/ajinkyajain23/rowproof) — it diffs two tables across Postgres, ClickHouse, and Snowflake, hash-and-bisects so it never has to pull a full table to the client (segments the key range, hashes each segment in one query per side, only fetches actual row detail for segments that disagree), and every difference it reports cites a specific rule — "this is DEC-1, the two sides declared different decimal scales" or "this is TS-3, one side's timestamp has no timezone" — not just "these two values are different." It also writes a self-contained HTML report meant to be attached to a migration sign-off, not just a CI log.

It's free and open source (Apache 2.0). Point it at your own Postgres/ClickHouse/Snowflake and it should run in a couple of minutes: https://github.com/ajinkyajain23/rowproof (`pip install rowproof`).

Anyone else have a favorite "row counts matched but the migration was still wrong" story? Curious what other silent-corruption patterns are common enough to be worth a named rule for.
