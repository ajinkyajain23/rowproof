# Benchmarks

**Status: accepted.** Correctness and memory pass cleanly; wall-clock time
is 1.8× over the 5-minute target for the reason documented below (a real
CPU-core ceiling on the shared 8-vCPU dev VM this ran on, not a software
defect) and has been accepted as-is rather than chased further on this
hardware. Re-verify on real deployment hardware before relying on the
5-minute number specifically.

## 100M-row Postgres ↔ ClickHouse diff (spec §10, §13 M2)

**Hardware:** Intel Core i5-11300H (4 physical / 8 logical cores) @ 3.10GHz,
16 GB RAM. Both Postgres 16 and ClickHouse 24.8 ran as Docker Desktop
containers sharing one 8-vCPU / 7.65 GB Docker Desktop VM on that same
laptop — i.e. the databases and the `tablediff` client itself all
contended for the same physical cores. This is deliberately closer to
"a developer's laptop" than "a provisioned benchmark server" — genuinely
modest hardware, not a favourable case.

**Setup:** `bench_source` (Postgres, `id bigint primary key, val text,
amount numeric(12,2), created_at timestamptz`) and `bench_target`
(ClickHouse, matching types, `ENGINE = MergeTree ORDER BY id`), each
loaded with 100,000,000 rows of matching data, then 1,000 differences
introduced on the ClickHouse side: 400 rows deleted (missing in target),
300 rows' `amount` changed, 300 extra rows inserted beyond the source's
id range. Diffed via the real CLI (`tablediff diff ... --key id`, JSON
output), not by calling `hashdiff.diff()` directly — an actual subprocess,
measuring wall-clock time and the **client** process's peak RSS
(`psutil`, sampled every 0.2s, including any child processes).

### Result

| Metric | Result | Spec §13 M2 target | Verdict |
|---|---|---|---|
| Correctness | `missing_in_target=400, extra_in_target=300, changed=300` — exactly the 1,000 introduced differences, nothing else | exact | **PASS** |
| Peak client memory | 127.5 MB | < 500 MB | **PASS** |
| Wall-clock time | 536.8s (8.95 min) | < 5 min | **FAIL** — 1.8× over budget |

Full JSON result:
```json
{
  "source_count": 100000000,
  "target_count": 99999900,
  "missing_in_target": 400,
  "extra_in_target": 300,
  "changed": 300,
  "truncated": false,
  "segments_examined": 1000,
  "queries_per_side": 11501
}
```

### What it took to get a correct, fast-as-this-hardware-allows number

The first full run reported `changed: 4,586,252` (not 300) and took 88
minutes. Both problems were tracked down and fixed rather than papered
over:

1. **A data-generation bug, not a tablediff bug.** The benchmark's own
   ClickHouse seed data computed `amount` via `toDecimal64(x / 100.0, 2)`
   — float64 division, which isn't exact for values like 29/100, so
   `toDecimal64()` (which *truncates*, not rounds) silently produced
   `0.28` instead of `0.29` for a meaningful fraction of the 100,000
   distinct residues used. Confirmed at small scale (200k rows) before
   touching the full dataset. Fixed by using exact decimal arithmetic
   (`toDecimal64(x, 4) / 100`) instead of a float divisor. This alone
   cut `queries_per_side` from 167,194 to 11,501 — almost all of the
   original 88 minutes was tablediff correctly, expensively chasing
   down millions of genuine (if unintended) differences.

2. **`--threads` was documented (spec §7) but never implemented.**
   Every segment query — thousands of them for a 1000-segment table —
   ran on one connection per side, strictly sequentially. Reconnecting
   per query was already fixed (see the connector-swap commit), but
   there was still no way to run independent segment queries
   concurrently at all. Implemented real thread-pooled execution
   (`core/hashdiff.py`'s `_diff_segments_parallel`, `--threads N` on the
   CLI, default 4) — see that commit for the full design. This is what
   took the corrected-data run from an estimated ~14 minutes
   single-threaded down to 8.95 minutes.

### Why it's still over 5 minutes, and what would close the gap

Isolated measurement (20-40 repeated segment-stats queries, `ThreadPoolExecutor`)
showed only ~1.6× real speedup from 4 threads — not the naive 4× — and
*worse* throughput at 8 threads. `EXPLAIN (ANALYZE, BUFFERS)` on a single
segment query explains why: Postgres's own planner already uses 2
internal parallel workers per query (`Workers Planned: 2`), so 4
concurrent client threads become ~12 concurrent Postgres backend
processes, on top of ClickHouse's own container, all sharing 8 vCPUs.
Explicitly disabling Postgres's internal parallelism
(`max_parallel_workers_per_gather = 0`) to leave more headroom for
client-side concurrency was tested and made things very slightly
*worse*, not better — the bottleneck is genuinely available CPU cycles
for MD5-hashing ~100M rows' worth of normalised column data, not a
tunable contention parameter. This is fundamentally the same work spec
§4.1's hashdiff algorithm asks the database to do (`row_hash_expr`,
`aggregate_hash_expr`) — correctness requires it, and this specific
8-vCPU shared-with-both-databases laptop VM is close to saturated doing
it in parallel at all.

What would plausibly close the remaining ~1.8× gap without changing the
algorithm: more CPU cores (the parallel-worker/thread-count math above
scales close to linearly with real core count up to the point of
saturation), or running the client and the two databases on separate
machines/cores instead of one shared Docker Desktop VM. Both are
hardware/deployment changes, not algorithm or connector bugs — recorded
here rather than silently reported as a pass.

### Verifying it wasn't a fluke

`tests/integration/test_m0_acceptance.py::TestThreadedHashdiffMatchesSequential`
proves the parallel path (`threads=4` with connection pools) produces an
identical result — same counts, same row-diff keys — to the sequential
path on a smaller, real Postgres table with deliberately scattered
differences, so the 8.95-minute number above reflects genuinely correct
work, not a shortcut that happens to look fast.
