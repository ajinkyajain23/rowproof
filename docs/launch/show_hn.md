# Show HN draft

**Title:** Show HN: Rowproof – prove a database migration didn't lose or change a row (Postgres, ClickHouse, Snowflake)

**Body:**

Every team that's migrated or replicated a database has had the same moment: the migration "succeeded," but did every row actually make it across, unchanged? Row counts matching isn't proof — a changed timestamp precision, a NULL that became an empty string, or a UUID that lost its hyphens all pass a naive count check while quietly corrupting your data. Existing diff tools tell you *that* two tables differ; almost none tell you *why*, in a form you can hand to someone who has to sign off on the cutover.

Rowproof is a CLI that verifies two tables match across Postgres, ClickHouse, and Snowflake — exact counts, every difference cited against a published rule (a timestamp-precision mismatch and a NULL-vs-empty-string mismatch are different bugs, and it tells you which one you have), reproducible SQL for every query it ran, and a self-contained HTML report meant to be attached to a migration sign-off, not just a terminal exit code.

It uses the same hash-and-bisect idea as [reladiff](https://github.com/erezsh/reladiff) (excellent project, actively maintained, successor to data-diff) — split the key range into segments, hash each segment's normalised rows in one query per side, bisect the segments that disagree. Where it differs: every cross-engine type difference is governed by a published rule table and cited in the output, the output is a sign-off report rather than a diff count, and the CLI is the free core of a hosted reconciliation service (scheduled runs, history, alerts) rather than a standalone library.

Benchmark: 100M rows, verified correct, 127 MB peak client memory, 8.95 minutes on a laptop with both databases sharing the same 8 vCPUs.

Link: [github.com/&lt;org&gt;/rowproof — fill in before posting]

---

**First comment draft (post as the OP, right after submitting):**

The interesting part of building this wasn't the algorithm — hash-and-bisect is well-trodden ground. It was how many ways two databases disagree about what a "normalised" value even is, and how few of those disagreements show up until you run real SQL against a real server.

Building the ClickHouse connector, I found nine real bugs this way — not from reading docs, from running the generated SQL and comparing byte-for-byte against Postgres's output for the same value:

- `toString()` on a ClickHouse `Decimal` silently strips trailing zeros, so `12.50` and `12.5` render identically when the fixed scale actually matters for comparison.
- `%` on a wide `Decimal256` isn't integer modulo — a "the digit is even" check using `%` gave the wrong answer for values that were actually odd.
- Formatting a float in scientific notation was missing the literal `e` character entirely for one code path.
- `toString(round(x, n))` doesn't zero-pad — `round(3.0, 14)` renders as `'3'`, not 14 decimal places, so two numerically-equal floats could render as different strings.
- The exponent-formatting code crashed outright on *any* row where some other row in the same query had the value `0` — ClickHouse evaluates every branch of a `CASE`/`multiIf` for every row, not just the branch that's selected, so an unrelated row's zero blew up a query that was working fine for every other row.
- The same exponent formatter also silently truncated 3-digit exponents to 2 digits (`leftPad` shortens an already-longer string instead of leaving it alone) — `1e308` rendered as `...e+30`.
- Segmenting a table by key range used SQL syntax that's valid Postgres but not valid ClickHouse at all.
- `IS DISTINCT FROM` — used everywhere else in the join-based diff path — isn't usable in a ClickHouse `WHERE` clause outside a `JOIN ON`.
- `FULL OUTER JOIN` fills a non-nullable column's unmatched side with the type's zero value, not `NULL`, unless you explicitly wrap it — which meant "row exists on side A but not side B" was silently never detected for the common case of a non-nullable primary key.

None of these were guessable from documentation alone. Every one only showed up by actually running the SQL against a live ClickHouse instance and checking the output — which is exactly why "verify on real databases before calling a milestone done" ended up as a hard rule for this project rather than a nice-to-have.
