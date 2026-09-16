"""tablediff CLI — argparse stand-in for `typer` (see docs/DEV_ENVIRONMENT.md).
Flags, behavior and exit codes follow spec §7 exactly; only the argument
*parsing library* differs from the spec's chosen stack.
"""

from __future__ import annotations

import argparse
import sys

from tablediff.cli.render import render_json, render_terminal
from tablediff.cli.spec import parse_source_spec, resolve_source_spec
from tablediff.config import load_config
from tablediff.connectors.clickhouse import ClickHouseConnector
from tablediff.connectors.postgres import PostgresConnector
from tablediff.core.errors import TableDiffError
from tablediff.core.hashdiff import diff as run_hashdiff
from tablediff.core.hashdiff import explain as run_explain
from tablediff.core.joindiff import diff as run_joindiff
from tablediff.report.html import render_html

EXIT_MATCH = 0
EXIT_DIFFERENT = 1
EXIT_COULD_NOT_COMPARE = 2

def _snowflake_factory():
    # Imported lazily, not at module scope like Postgres/ClickHouse above
    # — spec §3/§9: Snowflake support is an optional extra
    # (`tablediff[snowflake]`) precisely because its driver is a "heavy
    # dependency" a Postgres/ClickHouse-only install shouldn't be forced
    # to carry. An eager top-level import would defeat that: it'd make
    # `snowflake-connector-python` a hard dependency of the whole CLI,
    # breaking every command for a user who installed the base package.
    try:
        from tablediff.connectors.snowflake import SnowflakeConnector
    except ImportError as e:
        raise TableDiffError(
            "Snowflake support requires the optional extra -- install with "
            "`pip install tablediff[snowflake]` (or `pipx install tablediff[snowflake]`)"
        ) from e
    return SnowflakeConnector()


_CONNECTOR_FACTORIES = {
    "postgres": PostgresConnector,
    "postgresql": PostgresConnector,
    "clickhouse": ClickHouseConnector,
    "ch": ClickHouseConnector,
    "snowflake": _snowflake_factory,
    "sf": _snowflake_factory,
}


def _make_connector(engine: str, verbose: bool):
    factory = _CONNECTOR_FACTORIES.get(engine)
    if factory is None:
        supported = ", ".join(sorted(set(_CONNECTOR_FACTORIES) - {"postgresql", "ch", "sf"}))
        raise TableDiffError(f"unsupported engine '{engine}' (supported: {supported})")
    connector = factory()
    if verbose and hasattr(connector, "on_query"):
        connector.on_query = lambda sql: print(f"[sql] {sql}", file=sys.stderr)
    return connector


def _parse_sample_pct(value: str) -> float:
    """spec §7: `--sample 1%` (the `%` is optional -- `--sample 1` means
    the same thing)."""
    text = value.strip().rstrip("%").strip()
    try:
        pct = float(text)
    except ValueError as e:
        raise argparse.ArgumentTypeError(f"--sample expects a percentage like '1%%', got {value!r}") from e
    if not (0 < pct <= 100):
        raise argparse.ArgumentTypeError(f"--sample must be between 0 and 100, got {value!r}")
    return pct


def _split_cols(value: str | None) -> list[str] | None:
    if not value:
        return None
    return [c.strip() for c in value.split(",") if c.strip()]


def _parse_column_map(value: str | None) -> dict | None:
    """spec §7: `--column-map a:b,c:d`."""
    if not value:
        return None
    result = {}
    for pair in value.split(","):
        pair = pair.strip()
        if not pair:
            continue
        src, _, tgt = pair.partition(":")
        result[src.strip()] = tgt.strip()
    return result or None


def _connect_pair(args, sql_log: list[str] | None = None):
    src_spec = parse_source_spec(args.source)
    tgt_spec = parse_source_spec(args.target)
    source = _make_connector(src_spec.engine, args.verbose)
    target = _make_connector(tgt_spec.engine, args.verbose)
    if sql_log is not None:
        for connector in (source, target):
            if hasattr(connector, "on_query"):
                previous = connector.on_query
                # spec §8.2: JSON output carries every generated SQL
                # statement too — reuse the exact same on_query hook
                # --verbose already installs (chaining onto it, so
                # --verbose logging to stderr keeps working unchanged)
                # rather than a second SQL-capturing mechanism.
                connector.on_query = (
                    lambda sql, _prev=previous: (_prev(sql) if _prev else None, sql_log.append(sql))
                )
    source.connect(src_spec.connect_dsn)
    target.connect(tgt_spec.connect_dsn)

    # spec §7: `--threads N` — extra, already-connected connectors per
    # side for core.hashdiff's parallel segment path (see its own
    # docstring on why genuine parallelism needs genuine extra
    # connections, and why this is still "one [fixed] connection per
    # side" in spirit, not a general pool). `explain` never sets
    # --threads, and `getattr` covers that instead of giving every
    # subcommand's argparser a `--threads` flag it doesn't use.
    threads = getattr(args, "threads", 1)
    source_pool: list = []
    target_pool: list = []
    if threads > 1:
        source_pool = [_make_connector(src_spec.engine, args.verbose) for _ in range(threads)]
        target_pool = [_make_connector(tgt_spec.engine, args.verbose) for _ in range(threads)]
        for connector in source_pool:
            connector.connect(src_spec.connect_dsn)
        for connector in target_pool:
            connector.connect(tgt_spec.connect_dsn)

    return source, target, src_spec.table_ref, tgt_spec.table_ref, source_pool, target_pool


def resolve_algorithm(requested: str, args) -> str:
    """spec §4.2: "Use [joindiff] automatically when both sides resolve to
    the same connection; force with --algorithm joindiff." "same
    connection" is decided from the parsed DSN (engine + host/port/user/
    database), not from table identity — two different tables in the same
    database is exactly the case joindiff is for.
    """
    if requested != "auto":
        return requested
    src_spec = parse_source_spec(args.source)
    tgt_spec = parse_source_spec(args.target)
    if src_spec.engine == tgt_spec.engine and src_spec.connect_dsn == tgt_spec.connect_dsn:
        return "joindiff"
    return "hashdiff"


def _normalise_kwargs(args) -> dict:
    return dict(
        trim=args.trim,
        case_insensitive=args.case_insensitive,
        float_precision=args.float_precision,
        assume_tz=args.assume_tz,
        column_map=_parse_column_map(args.column_map),
    )


def cmd_diff(args) -> int:
    source = target = None
    source_pool: list = []
    target_pool: list = []
    try:
        sql_log: list[str] = []
        source, target, source_ref, target_ref, source_pool, target_pool = _connect_pair(args, sql_log)
        key_columns = _split_cols(args.key)
        columns = _split_cols(args.columns)
        exclude = _split_cols(args.exclude)
        algorithm = resolve_algorithm(args.algorithm, args)
        sample = getattr(args, "sample", None)
        sample_rows = getattr(args, "sample_rows", None)

        if algorithm == "joindiff":
            if sample is not None or sample_rows is not None:
                # spec §4.3: "apply the same sampling predicate to both
                # sides ... then run hashdiff on the sample" -- sampling
                # is defined in terms of hashdiff's segmented approach,
                # not joindiff's single exact query; --algorithm auto
                # would have picked hashdiff already for two different
                # connections, so this only fires when the user forced
                # joindiff (or both sides really are the same connection)
                # while also asking to sample -- a clear error beats
                # silently ignoring the flag.
                raise TableDiffError(
                    "--sample/--sample-rows requires hashdiff (spec §4.3) -- "
                    "pass --algorithm hashdiff, or diff two different connections "
                    "so hashdiff is auto-selected"
                )
            result = run_joindiff(
                source, target, source_ref, target_ref,
                key_columns=key_columns, columns=columns, exclude=exclude,
                max_diff_rows=args.max_diff_rows,
                where=args.where, where_source=args.where_source, where_target=args.where_target,
                **_normalise_kwargs(args),
            )
        else:
            result = run_hashdiff(
                source,
                target,
                source_ref,
                target_ref,
                key_columns=key_columns,
                columns=columns,
                exclude=exclude,
                row_threshold=args.row_threshold,
                max_diff_rows=args.max_diff_rows,
                where=args.where,
                where_source=args.where_source,
                where_target=args.where_target,
                threads=args.threads,
                source_pool=source_pool,
                target_pool=target_pool,
                sample=sample,
                sample_rows=sample_rows,
                **_normalise_kwargs(args),
            )

        outputs = args.output or ["terminal"]
        for fmt in outputs:
            if fmt == "terminal":
                print(render_terminal(result))
            elif fmt == "json":
                text = render_json(result, sql_statements=sql_log)
                if args.json_path:
                    with open(args.json_path, "w") as f:
                        f.write(text)
                else:
                    print(text)
            elif fmt == "html":
                # spec §8.3: DSNs shown in the report must be redacted --
                # render_html does that itself (via cli.spec.redact_dsn),
                # so the *raw* connect_dsn is passed through here, same as
                # what actually connected (re-parsed rather than plumbed
                # out of _connect_pair, since parse_source_spec is pure
                # and cheap, and no other caller needs the DSN back).
                html_text = render_html(
                    result,
                    source_dsn=parse_source_spec(args.source).connect_dsn,
                    target_dsn=parse_source_spec(args.target).connect_dsn,
                    sql_statements=sql_log,
                )
                if not args.html_path:
                    raise TableDiffError("--output html requires --html-path PATH")
                with open(args.html_path, "w", encoding="utf-8") as f:
                    f.write(html_text)

        if args.fail_on == "none":
            return EXIT_MATCH
        if args.fail_on == "count":
            return EXIT_MATCH if result.source_count == result.target_count else EXIT_DIFFERENT
        return result.exit_code()

    except TableDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    except Exception as e:  # noqa: BLE001 - CLI boundary: never leak a traceback
        # NOTE: this used to re-raise when --verbose was set, meaning to
        # show a traceback for debugging. That was wrong: re-raising here
        # doesn't add a traceback to a clean report, it crashes the process
        # — Python's own unhandled-exception handler then prints the
        # traceback AND exits with status 1, not 2. A real network-kill
        # test (tests/integration/test_m0_acceptance.py) caught this doing
        # exactly that. Spec §13 M0's "kill network mid-run" bullet
        # requires exit 2 with no traceback UNCONDITIONALLY (it carves out
        # no --verbose exception) — M4's "traceback only under --verbose"
        # is a later, more permissive general policy; when it's built, it
        # needs to be `traceback.print_exc()` followed by returning
        # EXIT_COULD_NOT_COMPARE, never a bare `raise` here.
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()
        for connector in (*source_pool, *target_pool):
            connector.close()


def cmd_explain(args) -> int:
    source = target = None
    try:
        source, target, source_ref, target_ref, _source_pool, _target_pool = _connect_pair(args)
        key_columns = _split_cols(args.key)
        columns = _split_cols(args.columns)
        exclude = _split_cols(args.exclude)
        statements = run_explain(
            source, target, source_ref, target_ref,
            key_columns=key_columns, columns=columns, exclude=exclude,
            where=args.where, where_source=args.where_source, where_target=args.where_target,
            **_normalise_kwargs(args),
        )
        for stmt in statements:
            print(stmt)
        return EXIT_MATCH
    except TableDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    except Exception as e:  # noqa: BLE001
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


def _run_one_job(job, connections: dict, verbose: bool) -> int:
    """One table pair from a `run` config — spec §7: "run many table
    pairs; one report." Each job prints its own terminal block (so the
    "one report" is the concatenation, in config order) and the run's
    overall exit code is the worst of every job's own (spec's exit-code
    semantics — 0 match / 1 different / 2 could-not-compare — apply the
    same way per table; "worst" means 2 beats 1 beats 0, the same
    ordering the exit codes already have).
    """
    source = target = None
    try:
        src_spec = resolve_source_spec(job.source, connections)
        tgt_spec = resolve_source_spec(job.target, connections)
        source = _make_connector(src_spec.engine, verbose)
        target = _make_connector(tgt_spec.engine, verbose)
        source.connect(src_spec.connect_dsn)
        target.connect(tgt_spec.connect_dsn)

        algorithm = job.algorithm
        if algorithm == "auto":
            algorithm = (
                "joindiff"
                if src_spec.engine == tgt_spec.engine and src_spec.connect_dsn == tgt_spec.connect_dsn
                else "hashdiff"
            )

        shared_kwargs = dict(
            key_columns=job.key, columns=job.columns, exclude=job.exclude,
            where=job.where, where_source=job.where_source, where_target=job.where_target,
            trim=job.trim, case_insensitive=job.case_insensitive,
            float_precision=job.float_precision, assume_tz=job.assume_tz, column_map=job.column_map,
        )
        if algorithm == "joindiff":
            result = run_joindiff(
                source, target, src_spec.table_ref, tgt_spec.table_ref,
                max_diff_rows=job.max_diff_rows, **shared_kwargs,
            )
        else:
            result = run_hashdiff(
                source, target, src_spec.table_ref, tgt_spec.table_ref,
                row_threshold=job.row_threshold, max_diff_rows=job.max_diff_rows, **shared_kwargs,
            )

        print(render_terminal(result))
        print()
        if job.fail_on == "none":
            return EXIT_MATCH
        if job.fail_on == "count":
            return EXIT_MATCH if result.source_count == result.target_count else EXIT_DIFFERENT
        return result.exit_code()
    except TableDiffError as e:
        print(f"error ({job.source} -> {job.target}): {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    except Exception as e:  # noqa: BLE001 - never leak a traceback (see cmd_diff's note)
        print(f"error ({job.source} -> {job.target}): {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    finally:
        if source is not None:
            source.close()
        if target is not None:
            target.close()


def cmd_run(args) -> int:
    try:
        config = load_config(args.config)
    except TableDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE

    if not config.tables:
        print("error: config has no 'tables' entries", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE

    worst = EXIT_MATCH
    for job in config.tables:
        code = _run_one_job(job, config.connections, args.verbose)
        worst = max(worst, code)
    return worst


def cmd_connections_test(args) -> int:
    try:
        config = load_config(args.config)
    except TableDiffError as e:
        print(f"error: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE

    if args.name not in config.connections:
        print(
            f"error: unknown connection '{args.name}' (known: {', '.join(sorted(config.connections))})",
            file=sys.stderr,
        )
        return EXIT_COULD_NOT_COMPARE

    dsn = config.connections[args.name]
    engine = dsn.split("://", 1)[0] if "://" in dsn else ""
    connector = None
    try:
        connector = _make_connector(engine, False)
        connector.connect(dsn)
        connector.query("SELECT 1")
    except TableDiffError as e:
        print(f"error: connection '{args.name}' failed: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    except Exception as e:  # noqa: BLE001 - never leak a traceback (see cmd_diff's note)
        print(f"error: connection '{args.name}' failed: {e}", file=sys.stderr)
        return EXIT_COULD_NOT_COMPARE
    finally:
        if connector is not None:
            connector.close()

    print(f"{args.name}: ok")
    return EXIT_MATCH


def _add_common_args(p: argparse.ArgumentParser) -> None:
    p.add_argument("source")
    p.add_argument("target")
    p.add_argument("--key", help="comma-separated key columns (default: detected PK)")
    p.add_argument("--columns", help="only compare these columns")
    p.add_argument("--exclude", help="skip these columns")
    p.add_argument("--where", help="SQL filter applied to both sides before comparing")
    p.add_argument("--where-source", help="SQL filter for the source side only (overrides --where)")
    p.add_argument("--where-target", help="SQL filter for the target side only (overrides --where)")
    p.add_argument("--verbose", action="store_true", help="log every generated SQL statement")
    p.add_argument("--trim", action="store_true", help="STR-2: strip trailing whitespace before comparing text")
    p.add_argument("--case-insensitive", action="store_true", help="STR-2: lower-case text before comparing")
    p.add_argument("--float-precision", type=int, default=15, help="FLT-1: significant digits (default 15)")
    p.add_argument("--assume-tz", default="UTC", help="TS-3: timezone assumed for naive timestamps (default UTC)")
    p.add_argument("--column-map", help="rename columns when matching sides: source_col:target_col,...")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tablediff")
    sub = parser.add_subparsers(dest="command", required=True)

    diff_p = sub.add_parser("diff", help="verify two tables match")
    _add_common_args(diff_p)
    diff_p.add_argument("--algorithm", choices=["auto", "hashdiff", "joindiff"], default="auto")
    diff_p.add_argument(
        "--threads", type=int, default=4,
        help="parallel segment queries per side, hashdiff only (default 4)",
    )
    diff_p.add_argument("--row-threshold", type=int, default=1000)
    diff_p.add_argument("--max-diff-rows", type=int, default=10_000)
    sample_group = diff_p.add_mutually_exclusive_group()
    sample_group.add_argument(
        "--sample", type=_parse_sample_pct, metavar="PCT",
        help="diff a deterministic sample of rows, e.g. --sample 1%% (spec §4.3)",
    )
    sample_group.add_argument(
        "--sample-rows", type=int, metavar="N",
        help="diff a deterministic sample of approximately N rows",
    )
    diff_p.add_argument("--output", action="append", choices=["terminal", "json", "html"], default=None)
    diff_p.add_argument("--json-path")
    diff_p.add_argument("--html-path", help="file to write the HTML sign-off report to (spec §8.3)")
    diff_p.add_argument("--fail-on", choices=["none", "any", "count"], default="any")
    diff_p.set_defaults(func=cmd_diff)

    explain_p = sub.add_parser("explain", help="print the SQL tablediff would run; execute nothing")
    _add_common_args(explain_p)
    explain_p.set_defaults(func=cmd_explain)

    run_p = sub.add_parser("run", help="run every table pair in a config file; one report")
    run_p.add_argument("config", help="path to a tablediff YAML config file")
    run_p.add_argument("--verbose", action="store_true", help="log every generated SQL statement")
    run_p.set_defaults(func=cmd_run)

    connections_p = sub.add_parser("connections", help="work with a config file's named connections")
    connections_sub = connections_p.add_subparsers(dest="connections_command", required=True)
    connections_test_p = connections_sub.add_parser("test", help="check credentials for a named connection")
    connections_test_p.add_argument("name", help="connection name, as declared in the config file")
    connections_test_p.add_argument(
        "--config", default="tablediff.yaml", help="path to a tablediff YAML config file (default: tablediff.yaml)"
    )
    connections_test_p.set_defaults(func=cmd_connections_test)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
