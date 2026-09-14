# M2 Completion Report — ClickHouse

**Status: done, accepted** (one documented, accepted exception — see below).

Run against real Postgres 16 + ClickHouse 24.8 (Docker Desktop, this
machine). Environment: `TABLEDIFF_TEST_CH_DSN=clickhouse://default:clickhouse@127.0.0.1:8123/default`.

## SPEC §13 M2 acceptance criteria

| # | Criterion | Result | Proven by |
|---|---|---|---|
| 1 | Cross-engine hash equality test passes for every fixture string | **PASS** | `TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines` (5 fixtures) |
| 2 | Postgres `timestamptz(6)` vs ClickHouse `DateTime` → differences within the same second are not reported; a TS-2 note appears once | **PASS** | `TestTs2AcrossEngines::test_timestamptz_vs_datetime_same_second_is_not_reported` |
| 3 | Postgres `numeric(12,2)` vs ClickHouse `Decimal(12,4)` → compared at scale 2, DEC-1 note | **PASS** | `TestDecimalAcrossEngines::test_numeric_12_2_vs_decimal_12_4_compares_at_scale_2` |
| 4 | ClickHouse `Nullable(String)` NULL vs Postgres NULL → equal | **PASS** | `TestNullableAcrossEngines::test_clickhouse_nullable_string_null_equals_postgres_null` |
| 5 | 100M-row benchmark — correctness | **PASS** | manual run (see `docs/benchmarks.md`) — exactly the 1,000 introduced differences (400 missing, 300 extra, 300 changed), nothing else |
| 5 | 100M-row benchmark — memory under 500 MB | **PASS** | manual run — 127.5 MB peak client RSS |
| 5 | 100M-row benchmark — under 5 minutes | **FAIL** | manual run — 8.95 min (536.8s), 1.8× over budget. Root-caused to a genuine CPU-core ceiling on this shared 8-vCPU dev VM (Postgres already spawns 2 internal parallel workers per query, so 4 client threads become ~12 competing backend processes), not a fixable software inefficiency. Investigated and accepted — full writeup in [`benchmarks.md`](benchmarks.md). |

**Also covered** (SPEC §9's M2 milestone description, all real-server tested):
ClickHouse connector; ClickHouse key segmentation for UInt/String/UUID
keys (String and UUID were genuinely broken — hard syntax errors — before
this session, `TestClickHouseKeySegmentation`); `DateTime`/`DateTime64`
precision (TS-2); `Nullable()` handling; `LowCardinality`, `Enum8`/`Enum16`,
`Array`, `Map` (as text, UNK-1) types (`TestClickHouseSpecificTypes`);
`Float64`/FLT-1 (`TestFloatAcrossEngines`, added in a post-acceptance
review pass — see below).

## Post-acceptance review findings

A reviewer asked for real `--verbose` SQL against a ClickHouse table with
`Decimal`/`DateTime64`/`Float64`/`Nullable(String)` columns. Generating
that answer surfaced a real bug none of the M2 tests above had caught:
**`ClickHouseConnector._flt1_expr` (FLT-1) was broken** — no existing
test had ever compared an actual float *value*'s canonical rendering
cross-engine (the cross-engine hash-equality test only hashes plain
strings). Three separate defects, all fixed and verified against a real
server (commit `1cf57d7`):

1. The scientific-notation output was missing the literal `e` character
   entirely (`'3.14159+00'` instead of `'3.14159e+00'`).
2. `toString(round(x, decimals))` on a Float64 silently drops trailing
   zeros — the same class of bug as the earlier DEC-1 fix — so most
   values disagreed between engines even when numerically identical.
3. The exponent formatting crashed outright on any row containing a bare
   `0` in the same query (ClickHouse's columnar `CASE` evaluates every
   branch for every row even when a different branch's result is
   selected) and truncated a 3-digit exponent to 2 (`1e308` → `...e+30`).

None of this changes the SPEC §13 M2 PASS/FAIL table above — FLT-1 isn't
one of its named criteria — but it's real evidence that "M2 done" from a
test pass is not the same as "no more bugs," and is recorded here rather
than left out. New `TestFloatAcrossEngines` covers it going forward.

Also found, and *not* fixed (genuinely out of scope): pointing `tablediff
diff` at two ClickHouse tables in the **same** database auto-selects
`joindiff` (per spec §4.2's own routing rule — same engine, same
connection), and `core/joindiff.py` unconditionally emits Postgres's `IS
DISTINCT FROM` syntax, which ClickHouse rejects. This was never spec'd or
tested for ClickHouse (SPEC §13 M2's criteria are all about `hashdiff`,
the cross-engine algorithm; §4.2 defines joindiff as same-engine-only).
It fails *cleanly* — exit 2, a clear `error: ...` message, no traceback —
so it's not silent corruption, but a real user pointing joindiff-eligible
ClickHouse tables at each other will hit it. Worth a follow-up, not a
blocker for M2 as scoped.

## What was fixed to get here

1. **Real drivers.** `psycopg` (v3) and `clickhouse-connect` replaced the
   psql-subprocess and stdlib-HTTP stand-ins — psql wasn't even installed
   on this machine, so the old Postgres path couldn't run at all here.
2. **Two real product bugs**, found only because real SQL finally ran
   against real servers: ClickHouse's `toString(Decimal)` silently strips
   trailing zeros (`12.50` → `'12.5'`), and `%` on a wide `Decimal256`
   doesn't behave like integer modulo. Both fixed in
   `ClickHouseConnector._dec1_expr`, verified against the live server.
3. **ClickHouse key segmentation had no ClickHouse-specific handling at
   all** (SPEC §9) — `core/hashdiff.py` unconditionally emitted
   Postgres-only SQL (`COLLATE "C"`, `CAST(... AS TEXT) COLLATE "C"`,
   `ORDER BY RANDOM()`) that ClickHouse rejects outright. Fixed by
   branching on `connector.engine`; new tests cover UInt64, String, and
   UUID ClickHouse keys end to end.
4. **Persistent connections.** Both connectors were reconnecting from
   scratch on every single query (a carryover from the old
   subprocess/HTTP stand-ins) — measured ~17–20ms of pure overhead per
   query. Now one real connection/client per side, held open for the
   whole diff.
5. **`--threads` implemented.** Spec §7 documents `--threads N: parallel
   segment queries per side` but it was never built — every segment
   query ran strictly sequentially. Implemented real thread-pooled
   parallel execution (`core/hashdiff.py`'s `_diff_segments_parallel`);
   proven to produce identical results to the sequential path (see
   `TestThreadedHashdiffMatchesSequential`).
6. **100M-row benchmark**, including catching and fixing a bug in the
   benchmark's *own* seed data (float-division imprecision inflated
   "differences" from 1,000 to 4.58 million before the real bugs above
   were even reached) — full investigation in
   [`docs/benchmarks.md`](benchmarks.md).
7. **Three real FLT-1 bugs** (see "Post-acceptance review findings" above).

## Commits this session

```
1cf57d7 Fix three real bugs in ClickHouse FLT-1 rendering, found by review request
28cd197 Add M2 completion report
4b682c2 Accept M2 as done — 100M-row benchmark's 5-min target waived (hardware-bound)
c91ff1b Record 100M-row benchmark (spec §13 M2) — correct and fast, not under 5 min
34203ad Implement --threads: parallel segment queries per side (spec §7)
4311a4d Add real-server tests for ClickHouse Enum8/Array/Map/LowCardinality/Nullable
7ba5537 Use one persistent connection per side instead of reconnecting per query
373846f ClickHouse key segmentation: fix hard-coded Postgres-only SQL (SPEC §9)
6545321 Replace psql and stdlib-HTTP stand-ins with real psycopg / clickhouse-connect
b9a1c0f Import M0-M2 code from cloud session
```

## Full test suite

**211 passed, 0 skipped** — every test in `tests/integration/test_clickhouse_real.py`
runs for real (none skip for an unreachable server anymore). Includes the
two new `TestFloatAcrossEngines` tests added by the FLT-1 fix above.

```
============================= test session starts =============================
platform win32 -- Python 3.12.10, pytest-9.1.1, pluggy-1.6.0 -- C:\dev\tablediff\.venv\Scripts\python.exe
cachedir: .pytest_cache
rootdir: C:\dev\tablediff
configfile: pyproject.toml
testpaths: tests
collecting ... collected 211 items

tests/integration/test_clickhouse_real.py::TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines[hello] PASSED [  0%]
tests/integration/test_clickhouse_real.py::TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines[] PASSED [  0%]
tests/integration/test_clickhouse_real.py::TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines[row-2] PASSED [  1%]
tests/integration/test_clickhouse_real.py::TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines[12.50] PASSED [  1%]
tests/integration/test_clickhouse_real.py::TestCrossEngineHashEqualityLive::test_same_fixture_string_hashes_equal_on_both_engines[unicode: caf\xe9 \u2603] PASSED [  2%]
tests/integration/test_clickhouse_real.py::TestTs2AcrossEngines::test_timestamptz_vs_datetime_same_second_is_not_reported PASSED [  2%]
tests/integration/test_clickhouse_real.py::TestDecimalAcrossEngines::test_numeric_12_2_vs_decimal_12_4_compares_at_scale_2 PASSED [  3%]
tests/integration/test_clickhouse_real.py::TestFloatAcrossEngines::test_float_values_match_across_engines_including_edge_cases PASSED [  3%]
tests/integration/test_clickhouse_real.py::TestFloatAcrossEngines::test_a_genuinely_different_float_is_still_reported_changed PASSED [  4%]
tests/integration/test_clickhouse_real.py::TestNullableAcrossEngines::test_clickhouse_nullable_string_null_equals_postgres_null PASSED [  4%]
tests/integration/test_clickhouse_real.py::TestClickHouseKeySegmentation::test_uint64_key_segments_and_matches PASSED [  5%]
tests/integration/test_clickhouse_real.py::TestClickHouseKeySegmentation::test_string_key_segments_and_matches PASSED [  5%]
tests/integration/test_clickhouse_real.py::TestClickHouseKeySegmentation::test_uuid_key_segments_and_matches PASSED [  6%]
tests/integration/test_clickhouse_real.py::TestClickHouseSpecificTypes::test_enum_array_map_lowcardinality_nullable_self_diff_matches PASSED [  6%]
tests/integration/test_clickhouse_real.py::TestClickHouseSpecificTypes::test_enum_array_map_reports_a_real_change PASSED [  7%]
tests/integration/test_clickhouse_real.py::TestCliSupportsClickHouse::test_cli_diff_postgres_to_clickhouse_returns_exit_0_on_match PASSED [  7%]
tests/integration/test_joindiff_real.py::TestJoindiffAgainstRealPostgres::test_identical_tables_match PASSED [  8%]
tests/integration/test_joindiff_real.py::TestJoindiffAgainstRealPostgres::test_missing_extra_and_changed_rows_in_a_single_query PASSED [  8%]
tests/integration/test_joindiff_real.py::TestJoindiffAgainstRealPostgres::test_dec_1_pair_scale_applies_inside_the_join PASSED [  9%]
tests/integration/test_joindiff_real.py::TestJoindiffAgainstRealPostgres::test_max_diff_rows_bounds_the_detail_fetch_with_exact_counts PASSED [  9%]
tests/integration/test_joindiff_real.py::TestJoindiffAgainstRealPostgres::test_where_filters_both_sides_inside_the_join PASSED [  9%]
tests/integration/test_m0_acceptance.py::TestCliExitCodesAgainstRealPostgres::test_cli_diff_returns_exit_0_when_tables_match PASSED [ 10%]
tests/integration/test_m0_acceptance.py::TestCliExitCodesAgainstRealPostgres::test_cli_diff_returns_exit_1_when_tables_differ PASSED [ 10%]
tests/integration/test_m0_acceptance.py::TestIdenticalOneMillionRows::test_two_identical_1m_row_tables_match_exit_0_under_10s PASSED [ 11%]
tests/integration/test_m0_acceptance.py::TestDeleteUpdateInsert::test_delete_10_update_10_insert_10_reported_and_classified PASSED [ 11%]
tests/integration/test_m0_acceptance.py::TestEmptyTableEdgeCases::test_source_empty_target_has_rows_reports_all_as_extra PASSED [ 12%]
tests/integration/test_m0_acceptance.py::TestEmptyTableEdgeCases::test_target_empty_source_has_rows_reports_all_as_missing PASSED [ 12%]
tests/integration/test_m0_acceptance.py::TestEmptyTableEdgeCases::test_both_sides_empty_is_a_match PASSED [ 13%]
tests/integration/test_m0_acceptance.py::TestColumnsAndExclude::test_exclude_hides_a_differing_column_from_the_diff PASSED [ 13%]
tests/integration/test_m0_acceptance.py::TestColumnsAndExclude::test_columns_restricts_comparison_to_the_named_set PASSED [ 14%]
tests/integration/test_m0_acceptance.py::TestWhereFilteringReal::test_where_excludes_rows_on_both_sides_real_postgres PASSED [ 14%]
tests/integration/test_m0_acceptance.py::TestWhereFilteringReal::test_where_source_and_where_target_differ_per_side_real_postgres PASSED [ 15%]
tests/integration/test_m0_acceptance.py::TestWhereFilteringReal::test_duplicate_key_scoped_by_where_real_postgres PASSED [ 15%]
tests/integration/test_m0_acceptance.py::TestWhereFilteringReal::test_cli_where_flag_end_to_end PASSED [ 16%]
tests/integration/test_m0_acceptance.py::TestMixedCaseTextKeysReal::test_mixed_case_text_keys_diff_correctly_under_locale_aware_collation PASSED [ 16%]
tests/integration/test_m0_acceptance.py::TestCompositeKeyReal::test_composite_key_tenant_id_order_id PASSED [ 17%]
tests/integration/test_m0_acceptance.py::TestTextColumnsWithLeadingZerosAreNotCorrupted::test_leading_zero_text_value_is_reported_intact_not_as_a_corrupted_int PASSED [ 17%]
tests/integration/test_m0_acceptance.py::TestUuidKeySegmentBalance::test_uuid_key_no_segment_holds_more_than_2x_average PASSED [ 18%]
tests/integration/test_m0_acceptance.py::TestSegmentQueriesUsePkIndex::test_uuid_key_segment_query_uses_pk_index PASSED [ 18%]
tests/integration/test_m0_acceptance.py::TestSegmentQueriesUsePkIndex::test_text_key_segment_query_uses_pk_index_under_default_collation PASSED [ 18%]
tests/integration/test_m0_acceptance.py::TestNoPrimaryKeyExitsTwo::test_table_without_pk_and_no_key_flag_exits_2_with_clear_message PASSED [ 19%]
tests/integration/test_m0_acceptance.py::TestNoPrimaryKeyExitsTwo::test_still_clean_exit_2_no_traceback_when_verbose_is_on PASSED [ 19%]
tests/integration/test_m0_acceptance.py::TestNonUniqueKeyExitsTwo::test_non_unique_key_exits_2_and_shows_duplicate_example PASSED [ 20%]
tests/integration/test_m0_acceptance.py::TestExplainExecutesNoDataQueries::test_explain_prints_sql_and_touches_no_row_data PASSED [ 20%]
tests/integration/test_m0_acceptance.py::TestNetworkDropMidRun::test_postgres_actually_killed_mid_run_is_a_clean_error_no_traceback PASSED [ 21%]
tests/integration/test_m0_acceptance.py::TestPasswordNeverLogged::test_password_never_appears_in_any_output_including_verbose PASSED [ 21%]
tests/integration/test_m0_acceptance.py::TestThreadedHashdiffMatchesSequential::test_parallel_result_matches_sequential_result_exactly PASSED [ 22%]
tests/integration/test_m0_acceptance.py::TestThreadedHashdiffMatchesSequential::test_threads_flag_without_a_pool_falls_back_to_sequential PASSED [ 22%]
tests/integration/test_m1_cli.py::TestAlgorithmAutoSelection::test_auto_picks_joindiff_when_both_sides_are_the_same_database PASSED [ 23%]
tests/integration/test_m1_cli.py::TestAlgorithmAutoSelection::test_auto_picks_hashdiff_when_databases_differ PASSED [ 23%]
tests/integration/test_m1_cli.py::TestAlgorithmAutoSelection::test_algorithm_hashdiff_forces_hashdiff_even_on_the_same_database PASSED [ 24%]
tests/integration/test_m1_cli.py::TestNormalisationFlagsEndToEnd::test_trim_flag_makes_trailing_whitespace_match PASSED [ 24%]
tests/integration/test_m1_cli.py::TestNormalisationFlagsEndToEnd::test_column_map_flag_matches_differently_named_columns PASSED [ 25%]
tests/integration/test_m1_cli.py::TestNullRenderedAsNullNotBackslashN::test_terminal_output_shows_null_not_backslash_n PASSED [ 25%]
tests/integration/test_m1_cli.py::TestNullRenderedAsNullNotBackslashN::test_json_output_shows_null_not_backslash_n PASSED [ 26%]
tests/integration/test_m1_cli.py::TestJsonOutputM1Fields::test_json_output_carries_excluded_columns_timings_and_sql PASSED [ 26%]
tests/integration/test_m1_fixtures.py::TestTimestampFixtures::test_tz_aware_converts_to_utc_iso8601 PASSED [ 27%]
tests/integration/test_m1_fixtures.py::TestTimestampFixtures::test_naive_timestamp_rendered_as_is_with_z_suffix_and_warns_once PASSED [ 27%]
tests/integration/test_m1_fixtures.py::TestTimestampFixtures::test_min_and_max_timestamps PASSED [ 27%]
tests/integration/test_m1_fixtures.py::TestTs2PrecisionMismatch::test_truncating_to_lower_precision_makes_same_second_a_match PASSED [ 28%]
tests/integration/test_m1_fixtures.py::TestTs2PrecisionMismatch::test_reports_changed_with_ts2_citation_when_truncated_values_still_differ PASSED [ 28%]
tests/integration/test_m1_fixtures.py::TestTs2PrecisionMismatch::test_rounds_half_up_at_lower_precision_not_truncates PASSED [ 29%]
tests/integration/test_m1_fixtures.py::TestTs2TimePrecisionMismatch::test_time_precision_pair_rounds_half_up_and_matches PASSED [ 29%]
tests/integration/test_m1_fixtures.py::TestTs2TimePrecisionMismatch::test_time_precision_pair_reports_changed_when_rounded_values_still_differ PASSED [ 30%]
tests/integration/test_m1_fixtures.py::TestDecimalFixtures::test_pair_compares_at_min_scale_of_the_two_sides PASSED [ 30%]
tests/integration/test_m1_fixtures.py::TestDecimalFixtures::test_negative_zero_renders_as_zero PASSED [ 31%]
tests/integration/test_m1_fixtures.py::TestDecimalFixtures::test_round_half_even_at_exact_midpoints_not_round_half_away_from_zero PASSED [ 31%]
tests/integration/test_m1_fixtures.py::TestFloatFixtures::test_default_15_significant_figures_scientific_notation PASSED [ 32%]
tests/integration/test_m1_fixtures.py::TestFloatFixtures::test_negative_zero_and_positive_zero_render_identically PASSED [ 32%]
tests/integration/test_m1_fixtures.py::TestFloatFixtures::test_nan_both_sides_nan_is_equal PASSED [ 33%]
tests/integration/test_m1_fixtures.py::TestFloatFixtures::test_infinity_renders_literally PASSED [ 33%]
tests/integration/test_m1_fixtures.py::TestFloatFixtures::test_float_precision_override_changes_significant_digit_count PASSED [ 34%]
tests/integration/test_m1_fixtures.py::TestStringFixtures::test_str1_no_trim_no_case_fold_by_default PASSED [ 34%]
tests/integration/test_m1_fixtures.py::TestStringFixtures::test_str2_trim_makes_trailing_whitespace_variant_match PASSED [ 35%]
tests/integration/test_m1_fixtures.py::TestStringFixtures::test_strasse_vs_strasse_differ_even_case_insensitive PASSED [ 35%]
tests/integration/test_m1_fixtures.py::TestStringFixtures::test_emoji_rtl_and_10000_char_string_pass_through_unchanged PASSED [ 36%]
tests/integration/test_m1_fixtures.py::TestStringFixtures::test_nul_byte_cannot_be_stored_in_postgres_text PASSED [ 36%]
tests/integration/test_m1_fixtures.py::TestNullFixtures::test_null_vs_empty_string_vs_literal_null_text_all_distinct PASSED [ 36%]
tests/integration/test_m1_fixtures.py::TestNullFixtures::test_null_vs_zero_int PASSED [ 37%]
tests/integration/test_m1_fixtures.py::TestBooleanFixtures::test_true_false_render_as_literal_words PASSED [ 37%]
tests/integration/test_m1_fixtures.py::TestBooleanFixtures::test_boolean_vs_integer_0_1_is_excluded_as_incompatible_not_silently_mapped PASSED [ 38%]
tests/integration/test_m1_fixtures.py::TestUuidFixtures::test_uuid_renders_lower_case_hyphenated_regardless_of_input_case PASSED [ 38%]
tests/integration/test_m1_fixtures.py::TestUuidFixtures::test_uuid_without_hyphens_input_still_renders_hyphenated PASSED [ 39%]
tests/integration/test_m1_fixtures.py::TestArrayFixtures::test_int_array_renders_bracket_format_order_sensitive PASSED [ 39%]
tests/integration/test_m1_fixtures.py::TestArrayFixtures::test_empty_array_vs_null_stay_distinct PASSED [ 40%]
tests/integration/test_m1_fixtures.py::TestArrayFixtures::test_text_array_with_null_element PASSED [ 40%]
tests/integration/test_m1_fixtures.py::TestJsonFixtures::test_jsonb_key_order_is_engine_side_canonicalised PASSED [ 41%]
tests/integration/test_m1_fixtures.py::TestJsonFixtures::test_plain_json_also_canonicalised_via_jsonb_cast PASSED [ 41%]
tests/integration/test_m1_fixtures.py::TestEnumFixtures::test_enum_renders_as_label_text PASSED [ 42%]
tests/integration/test_m1_fixtures.py::TestBinaryFixtures::test_bytea_renders_lower_case_hex_without_backslash_x_prefix PASSED [ 42%]
tests/integration/test_m1_fixtures.py::TestDateTimeFixtures::test_date_renders_iso PASSED [ 43%]
tests/integration/test_m1_fixtures.py::TestDateTimeFixtures::test_time_renders_with_six_fractional_digits PASSED [ 43%]
tests/integration/test_m1_fixtures.py::TestUnknownTypeNeverFailsARun::test_unknown_type_falls_back_to_text_cast_and_warns_once PASSED [ 44%]
tests/integration/test_run_and_connections.py::TestRunCommand::test_run_reports_match_for_every_table_pair PASSED [ 44%]
tests/integration/test_run_and_connections.py::TestRunCommand::test_run_exit_code_is_worst_across_all_table_pairs PASSED [ 45%]
tests/integration/test_run_and_connections.py::TestRunCommand::test_run_with_env_var_secret_in_dsn PASSED [ 45%]
tests/integration/test_run_and_connections.py::TestConnectionsTestCommand::test_connections_test_reports_ok_for_a_reachable_connection PASSED [ 45%]
tests/integration/test_run_and_connections.py::TestConnectionsTestCommand::test_connections_test_fails_clearly_for_a_bad_connection PASSED [ 46%]
tests/integration/test_run_and_connections.py::TestConnectionsTestCommand::test_connections_test_unknown_name_exits_2 PASSED [ 46%]
tests/unit/test_cli_spec.py::test_parses_schema_and_table PASSED         [ 47%]
tests/unit/test_cli_spec.py::test_table_without_explicit_schema_defaults_to_none PASSED [ 47%]
tests/unit/test_cli_spec.py::test_missing_table_raises_clear_error PASSED [ 48%]
tests/unit/test_clickhouse_connector.py::TestUnwrap::test_bare_type_is_not_nullable PASSED [ 48%]
tests/unit/test_clickhouse_connector.py::TestUnwrap::test_nullable_wrapper_strips_and_flags_nullable PASSED [ 49%]
tests/unit/test_clickhouse_connector.py::TestUnwrap::test_low_cardinality_wrapper_strips_without_flagging_nullable PASSED [ 49%]
tests/unit/test_clickhouse_connector.py::TestUnwrap::test_low_cardinality_of_nullable_strips_both_and_flags_nullable PASSED [ 50%]
tests/unit/test_clickhouse_connector.py::TestUnwrap::test_wrapper_stripping_does_not_touch_unrelated_parens PASSED [ 50%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_every_int_width_and_signedness_maps_to_int_1 PASSED [ 51%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_float32_and_float64_map_to_flt_1 PASSED [ 51%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_decimal_reports_scale_and_maps_to_dec_1 PASSED [ 52%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_decimal_with_no_explicit_scale_defaults_to_zero PASSED [ 52%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_fixed_width_decimal_variants_report_their_scale PASSED [ 53%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_datetime_maps_to_ts_1_family_with_precision_zero PASSED [ 53%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_datetime64_reports_its_precision_and_maps_to_ts_1_family PASSED [ 54%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_datetime64_with_timezone_argument_still_parses_precision PASSED [ 54%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_date_and_date32_map_to_date_1 PASSED [ 54%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_uuid_maps_to_uuid_1 PASSED [ 55%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_string_and_fixedstring_map_to_str_1 PASSED [ 55%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_bool_maps_to_bool_1 PASSED [ 56%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_enum8_and_enum16_map_to_enum_1 PASSED [ 56%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_array_maps_to_arr_1 PASSED [ 57%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_map_falls_back_to_unk_1_per_spec PASSED [ 57%]
tests/unit/test_clickhouse_connector.py::TestResolve::test_tuple_falls_back_to_unk_1 PASSED [ 58%]
tests/unit/test_clickhouse_connector.py::TestGetSchemaAndPrimaryKey::test_get_schema_maps_system_columns_rows_into_columns PASSED [ 58%]
tests/unit/test_clickhouse_connector.py::TestGetSchemaAndPrimaryKey::test_get_primary_key_returns_none_when_no_rows PASSED [ 59%]
tests/unit/test_clickhouse_connector.py::TestGetSchemaAndPrimaryKey::test_get_primary_key_returns_names_in_order PASSED [ 59%]
tests/unit/test_clickhouse_connector.py::TestNormaliseExprShape::test_int_1_wraps_null_check_when_nullable PASSED [ 60%]
tests/unit/test_clickhouse_connector.py::TestNormaliseExprShape::test_row_hash_expr_single_column_skips_separator PASSED [ 60%]
tests/unit/test_clickhouse_connector.py::TestNormaliseExprShape::test_row_hash_expr_multi_column_joins_with_unit_separator PASSED [ 61%]
tests/unit/test_clickhouse_connector.py::TestNormaliseExprShape::test_quote_identifier_escapes_backtick PASSED [ 61%]
tests/unit/test_clickhouse_connector.py::TestNormaliseExprShape::test_quote_literal_escapes_quotes_and_backslashes PASSED [ 62%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[hello] PASSED [ 62%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[] PASSED [ 63%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[row-2] PASSED [ 63%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[2024-03-01T04:30:00.000000Z] PASSED [ 63%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[\\N] PASSED [ 64%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[12.50] PASSED [ 64%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa] PASSED [ 65%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_postgres_and_simulated_clickhouse_agree[unicode: caf\xe9 \u2603] PASSED [ 65%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_regression_values_match_the_empirically_confirmed_psql_round_trip PASSED [ 66%]
tests/unit/test_clickhouse_hash_reference.py::TestCrossEngineHashReference::test_byte_order_actually_matters_here PASSED [ 66%]
tests/unit/test_column_matching.py::test_matched_columns_present_on_both_sides PASSED [ 67%]
tests/unit/test_column_matching.py::test_case_insensitive_name_matching PASSED [ 67%]
tests/unit/test_column_matching.py::test_column_map_override PASSED      [ 68%]
tests/unit/test_column_matching.py::test_source_only_column_excluded_as_schema_difference PASSED [ 68%]
tests/unit/test_column_matching.py::test_incompatible_type_families_excluded_and_reported_loudly PASSED [ 69%]
tests/unit/test_column_matching.py::test_compatible_text_type_families_are_fine PASSED [ 69%]
tests/unit/test_column_matching.py::test_unk_1_never_excludes_a_column PASSED [ 70%]
tests/unit/test_column_matching.py::test_dec_1_uses_min_scale_of_the_pair_and_warns PASSED [ 70%]
tests/unit/test_column_matching.py::test_dec_1_same_scale_no_warning PASSED [ 71%]
tests/unit/test_column_matching.py::test_ts_2_upgrade_when_precision_differs PASSED [ 71%]
tests/unit/test_column_matching.py::test_no_ts_2_upgrade_when_precision_matches PASSED [ 72%]
tests/unit/test_column_matching.py::test_str_2_promotion_when_trim_or_case_insensitive_active PASSED [ 72%]
tests/unit/test_column_matching.py::test_no_str_2_promotion_by_default PASSED [ 72%]
tests/unit/test_config.py::test_loads_connections_and_tables PASSED      [ 73%]
tests/unit/test_config.py::test_connection_as_mapping_with_dsn_key PASSED [ 73%]
tests/unit/test_config.py::test_env_var_substitution_in_dsn PASSED       [ 74%]
tests/unit/test_config.py::test_missing_env_var_raises_clear_error PASSED [ 74%]
tests/unit/test_config.py::test_table_missing_source_or_target_raises PASSED [ 75%]
tests/unit/test_config.py::test_key_columns_accept_comma_separated_string_or_list PASSED [ 75%]
tests/unit/test_config.py::test_missing_file_raises_table_diff_error PASSED [ 76%]
tests/unit/test_config.py::test_defaults_applied_when_options_omitted PASSED [ 76%]
tests/unit/test_hashdiff_core.py::TestIdenticalTables::test_identical_1000_row_tables_match PASSED [ 77%]
tests/unit/test_hashdiff_core.py::TestMissingExtraChanged::test_delete_update_insert_are_all_reported_and_classified PASSED [ 77%]
tests/unit/test_hashdiff_core.py::TestCompositeKey::test_composite_key_tenant_and_order_id PASSED [ 78%]
tests/unit/test_hashdiff_core.py::TestUuidKey::test_uuid_key_table_diffs_correctly PASSED [ 78%]
tests/unit/test_hashdiff_core.py::TestNoPrimaryKey::test_no_pk_and_no_explicit_key_raises PASSED [ 79%]
tests/unit/test_hashdiff_core.py::TestNonUniqueKey::test_non_unique_explicit_key_raises_with_example PASSED [ 79%]
tests/unit/test_hashdiff_core.py::TestMaxDiffRowsCap::test_diff_row_collection_is_capped_and_marked_truncated PASSED [ 80%]
tests/unit/test_hashdiff_core.py::TestExplainDoesNotExecute::test_explain_returns_sql_without_running_against_source_or_target PASSED [ 80%]
tests/unit/test_hashdiff_core.py::TestNonNumericKeySegmentationCoverage::test_target_only_row_below_source_min_with_text_key PASSED [ 81%]
tests/unit/test_hashdiff_core.py::TestNonNumericKeySegmentationCoverage::test_30k_text_keys_three_smallest_changed_reports_exactly_three PASSED [ 81%]
tests/unit/test_hashdiff_core.py::TestWhereFiltering::test_where_excludes_rows_from_comparison_on_both_sides PASSED [ 81%]
tests/unit/test_hashdiff_core.py::TestWhereFiltering::test_where_source_and_where_target_can_differ_per_side PASSED [ 82%]
tests/unit/test_hashdiff_core.py::TestWhereFiltering::test_duplicate_key_check_is_scoped_by_where_not_the_whole_table PASSED [ 82%]
tests/unit/test_joindiff_core.py::test_identical_tables_match PASSED     [ 83%]
tests/unit/test_joindiff_core.py::test_missing_extra_and_changed_all_detected_in_one_query PASSED [ 83%]
tests/unit/test_joindiff_core.py::test_algorithm_field_is_joindiff PASSED [ 84%]
tests/unit/test_joindiff_core.py::test_where_filters_both_sides PASSED   [ 84%]
tests/unit/test_joindiff_core.py::test_max_diff_rows_caps_row_list_but_counts_stay_exact PASSED [ 85%]
tests/unit/test_joindiff_core.py::test_all_rows_differ_client_stays_bounded PASSED [ 85%]
tests/unit/test_normalisation.py::test_integer_types_pick_int_1 PASSED   [ 86%]
tests/unit/test_normalisation.py::test_text_types_pick_str_1 PASSED      [ 86%]
tests/unit/test_normalisation.py::test_uuid_picks_uuid_1 PASSED          [ 87%]
tests/unit/test_normalisation.py::test_decimal_types_pick_dec_1 PASSED   [ 87%]
tests/unit/test_normalisation.py::test_float_types_pick_flt_1 PASSED     [ 88%]
tests/unit/test_normalisation.py::test_boolean_types_pick_bool_1 PASSED  [ 88%]
tests/unit/test_normalisation.py::test_date_picks_date_1 PASSED          [ 89%]
tests/unit/test_normalisation.py::test_time_types_pick_time_1 PASSED     [ 89%]
tests/unit/test_normalisation.py::test_timestamp_with_time_zone_picks_ts_1 PASSED [ 90%]
tests/unit/test_normalisation.py::test_naive_timestamp_picks_ts_3 PASSED [ 90%]
tests/unit/test_normalisation.py::test_bytea_picks_bin_1 PASSED          [ 90%]
tests/unit/test_normalisation.py::test_json_types_pick_json_1 PASSED     [ 91%]
tests/unit/test_normalisation.py::test_enum_sentinel_picks_enum_1 PASSED [ 91%]
tests/unit/test_normalisation.py::test_array_sentinel_picks_arr_1 PASSED [ 92%]
tests/unit/test_normalisation.py::test_unmapped_type_falls_back_to_unk_1_and_never_raises PASSED [ 92%]
tests/unit/test_normalisation.py::test_type_matching_is_case_insensitive PASSED [ 93%]
tests/unit/test_render.py::test_display_maps_the_null_1_marker_to_null PASSED [ 93%]
tests/unit/test_render.py::test_display_leaves_other_values_untouched PASSED [ 94%]
tests/unit/test_render.py::test_terminal_output_shows_null_not_backslash_n PASSED [ 94%]
tests/unit/test_render.py::test_json_output_shows_null_not_backslash_n PASSED [ 95%]
tests/unit/test_render.py::test_real_backslash_n_in_the_middle_of_a_value_is_not_mangled PASSED [ 95%]
tests/unit/test_segmentation.py::test_compute_num_segments_respects_minimum_of_eight_for_small_tables PASSED [ 96%]
tests/unit/test_segmentation.py::test_compute_num_segments_scales_with_count PASSED [ 96%]
tests/unit/test_segmentation.py::test_compute_num_segments_empty_table_is_one_segment PASSED [ 97%]
tests/unit/test_segmentation.py::test_segment_numeric_range_covers_full_range_with_no_gaps_or_overlap PASSED [ 97%]
tests/unit/test_segmentation.py::test_segment_numeric_range_single_value_table_is_one_segment_spanning_it PASSED [ 98%]
tests/unit/test_segmentation.py::test_segment_numeric_range_balances_rows_for_dense_sequential_keys PASSED [ 98%]
tests/unit/test_segmentation.py::test_segment_by_samples_balances_uuid_keys PASSED [ 99%]
tests/unit/test_segmentation.py::test_segment_numeric_range_bounded_mode_never_exceeds_parent_hi PASSED [ 99%]
tests/unit/test_segmentation.py::test_segment_by_samples_handles_duplicate_sample_values PASSED [100%]

======================= 211 passed in 88.96s (0:01:28) ========================
```

## Next milestone

Per `HANDOVER.md`: M3 (Snowflake) has not been started and won't be
without being told.
