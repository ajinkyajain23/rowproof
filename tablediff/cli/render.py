"""Terminal + JSON rendering — plain-text stand-in for `rich` (see
docs/DEV_ENVIRONMENT.md). Behavior/content matches spec §8; only the
"tables and progress bars" styling is simplified.
"""

from __future__ import annotations

import json

from tablediff.core.models import DiffResult, TableRef


def _display(value):
    """A value as shown in row-level output (terminal and JSON): the
    NULL-1 canonical string (spec §6.1: "the literal `\\N`") is the
    internal SQL-comparison marker every normalise_expr wraps a NULL in —
    exactly what hashing/equality needs it to be — but showing a data
    engineer a raw `\\N` in a report reads as a mangled value, not a
    deliberate marker. Row *output* (this function) shows "NULL" instead;
    the underlying RowDiff/DiffResult model, the hash, and the
    IS DISTINCT FROM comparison all keep using the real `\\N` string
    completely unchanged — only this display step ever runs the swap, and
    only on a value that is exactly the marker, never a substring match.
    """
    if value == "\\N":
        return "NULL"
    return value


def render_terminal(result: DiffResult) -> str:
    lines = []
    lines.append(f"{result.source.qualified_name}  {result.source}  ->  {result.target}")
    lines.append(f"  rows           {result.source_count:<20} {result.target_count:<20}")
    lines.append(f"  key            {', '.join(result.key_columns)}")
    if result.excluded_columns:
        lines.append(f"  columns        {len(result.excluded_columns)} excluded ({', '.join(result.excluded_columns)})")
    lines.append(
        f"  algorithm      {result.algorithm.value} · {result.segments_examined} segments · "
        f"{result.queries_per_side} queries/side · {result.elapsed_seconds:.1f}s"
    )
    lines.append("")
    lines.append(f"  missing in target   {result.missing_in_target}")
    lines.append(f"  extra in target     {result.extra_in_target}")
    lines.append(f"  changed             {result.changed}")

    if result.row_diffs:
        lines.append("")
        for rd in result.row_diffs:
            key_str = ",".join(str(k) for k in rd.key)
            if rd.kind == "changed":
                for col, (sv, tv, rule) in rd.changes.items():
                    rule_note = f"  ({rule.value})" if rule else ""
                    lines.append(f"    key={key_str}  {col}  {_display(sv)!r} -> {_display(tv)!r}{rule_note}")
            else:
                lines.append(f"    key={key_str}  {rd.kind}")
        if result.truncated:
            lines.append(f"  ... row list truncated at {len(result.row_diffs)} rows (counts above are exact)")

    for w in result.warnings:
        lines.append(f"  warning: {w.message}")

    lines.append("")
    verdict = "MATCH" if result.is_match else "DIFFERENT"
    lines.append(f"  result   {verdict}   exit {result.exit_code()}")
    return "\n".join(lines)


def render_json(
    result: DiffResult,
    sql_statements: list[str] | None = None,
) -> str:
    """spec §8.2: "Stable schema, versioned (schema_version: 1). Contains
    everything in the terminal output plus every generated SQL statement
    (redacted), timings, and the warnings list."

    `sql_statements` is the caller's own query log (the CLI already wires
    every connector's `on_query` hook for `--verbose`; JSON output reuses
    exactly that same hook to collect this list rather than adding a
    second SQL-capturing mechanism) — SQL text itself never contains
    connection secrets (those live in the DSN, which no generated
    statement ever embeds), so "redacted" here is automatically satisfied
    by construction, not by a separate scrub step.
    """

    def default(o):
        if hasattr(o, "value"):  # Enum
            return o.value
        if isinstance(o, TableRef):
            return {"engine": o.engine, "database": o.database, "schema": o.schema, "table": o.table}
        raise TypeError(f"not JSON serialisable: {o!r}")

    payload = {
        "schema_version": 1,
        "source": result.source,
        "target": result.target,
        "key_columns": list(result.key_columns),
        "algorithm": result.algorithm,
        "source_count": result.source_count,
        "target_count": result.target_count,
        "missing_in_target": result.missing_in_target,
        "extra_in_target": result.extra_in_target,
        "changed": result.changed,
        "truncated": result.truncated,
        "is_match": result.is_match,
        "exit_code": result.exit_code(),
        "excluded_columns": list(result.excluded_columns),
        "segments_examined": result.segments_examined,
        "queries_per_side": result.queries_per_side,
        "timings": {"total_seconds": round(result.elapsed_seconds, 3)},
        "sql_statements": list(sql_statements or []),
        "warnings": [w.message for w in result.warnings],
        "row_diffs": [
            {
                "key": list(rd.key),
                "kind": rd.kind,
                "changes": {
                    col: {"source": _display(sv), "target": _display(tv), "rule": rule.value if rule else None}
                    for col, (sv, tv, rule) in rd.changes.items()
                },
            }
            for rd in result.row_diffs
        ],
    }
    return json.dumps(payload, default=default, indent=2)
