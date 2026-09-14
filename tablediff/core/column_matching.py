"""Column matching across sides — spec §6.2.

Pure Python (no database imports, spec §0 rule 4): given both sides'
resolved schemas and the candidate list of value-column names a user asked
to compare (post `--columns`/`--exclude`), this decides which columns
actually get hashed/compared, which get excluded and why, and computes the
*pair-level* facts DEC-1 and TS-2 need before `Connector.normalise_expr`
can render a canonical string — a single column's own scale/precision
isn't enough on its own; the canonical form must agree with whatever the
other side declares.

`core/hashdiff.py` calls `match_columns()` once per `diff()`/`explain()`
call, after both sides' schemas are known, and applies the result to both
`_Plan`s so they render matched columns identically.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, Warning as TdWarning
from tablediff.core.normalisation import pick_rule

# Coarse type families for spec §6.2's "families incompatible" check.
# Deliberately coarser than NormalisationRule: STR-1/STR-2 are the same
# family (STR-2 is just an opt-in *rendering* of STR-1, never a separate
# native type — see match_columns' STR-2 handling below), and TS-1/TS-2/
# TS-3 are the same family (TS-2 is a *precision-mismatch upgrade* of
# TS-1/TS-3, not a distinct native type either).
_FAMILY = {
    NormalisationRule.INT_1: "numeric",
    NormalisationRule.DEC_1: "numeric",
    NormalisationRule.FLT_1: "numeric",
    NormalisationRule.STR_1: "string",
    NormalisationRule.STR_2: "string",
    NormalisationRule.BOOL_1: "boolean",
    NormalisationRule.TS_1: "timestamp",
    NormalisationRule.TS_2: "timestamp",
    NormalisationRule.TS_3: "timestamp",
    NormalisationRule.DATE_1: "date",
    NormalisationRule.TIME_1: "time",
    NormalisationRule.UUID_1: "uuid",
    NormalisationRule.BIN_1: "binary",
    NormalisationRule.JSON_1: "json",
    NormalisationRule.ARR_1: "array",
    NormalisationRule.ENUM_1: "enum",
}


def _family(rule: NormalisationRule) -> str:
    return _FAMILY.get(rule, "unknown")


def _families_compatible(src_rule: NormalisationRule, tgt_rule: NormalisationRule) -> bool:
    # UNK-1 (spec §6.1): "Never fail a run because of an unknown type" —
    # an unmapped type is always compared as raw text against anything
    # rather than excluded, on either side.
    if src_rule is NormalisationRule.UNK_1 or tgt_rule is NormalisationRule.UNK_1:
        return True
    # spec §6.2: "A Postgres uuid against a Snowflake VARCHAR compares
    # after UUID-1" — a deliberate exception, since an engine with no
    # native uuid type (Snowflake) has no way to report one; the uuid
    # side and the plain-string side are compatible specifically because
    # UUID-1's own canonical form (lower-case, hyphenated) is exactly
    # what lets a text-stored uuid compare correctly against a native one.
    if {_family(src_rule), _family(tgt_rule)} == {"uuid", "string"}:
        return True
    return _family(src_rule) == _family(tgt_rule)


@dataclass(frozen=True)
class ColumnMatch:
    matched: list[str]  # column names compared on both sides, in candidate order
    excluded: list[str]  # column names excluded (one-sided or incompatible)
    rule_overrides: dict[str, NormalisationRule] = field(default_factory=dict)
    options: NormaliseOptions = field(default_factory=NormaliseOptions)
    warnings: list[TdWarning] = field(default_factory=list)


def match_columns(
    source_columns: dict[str, Column],
    target_columns: dict[str, Column],
    candidate_names: list[str],
    column_map: dict[str, str] | None = None,
    base_options: NormaliseOptions | None = None,
) -> ColumnMatch:
    """`candidate_names` are the source-side column names a user asked to
    compare (post `--columns`/`--exclude`); this matches each against the
    target side by name, case-insensitively, honouring an optional
    `column_map` (source name -> target name) override.
    """
    base = base_options or NormaliseOptions()
    tgt_by_lower = {name.lower(): name for name in target_columns}
    column_map = column_map or {}

    matched: list[str] = []
    excluded: list[str] = []
    rule_overrides: dict[str, NormalisationRule] = {}
    scale_overrides = dict(base.scale_overrides)
    precision_overrides = dict(base.precision_overrides)
    warnings: list[TdWarning] = []

    for name in candidate_names:
        src_col = source_columns.get(name)
        if src_col is None:
            # Not actually present on the source side (e.g. a stale
            # --columns entry) — nothing to match; leave it alone rather
            # than inventing a schema-difference warning about a column
            # that never existed.
            continue

        mapped = column_map.get(name)
        tgt_name = mapped if mapped is not None else tgt_by_lower.get(name.lower())
        tgt_col = target_columns.get(tgt_name) if tgt_name else None

        if tgt_col is None:
            excluded.append(name)
            warnings.append(
                TdWarning(
                    f"column {name!r} present only on the source side "
                    "— excluded from comparison (schema difference)",
                    column=name,
                )
            )
            continue

        src_rule = pick_rule(src_col.native_type)
        tgt_rule = pick_rule(tgt_col.native_type)

        if not _families_compatible(src_rule, tgt_rule):
            excluded.append(name)
            warnings.append(
                TdWarning(
                    f"column {name!r} excluded: incompatible types "
                    f"(source {src_col.native_type!r} vs target {tgt_col.native_type!r})",
                    column=name,
                )
            )
            continue

        matched.append(name)

        # UUID-1 (spec §6.2): when the pairing above was allowed through
        # specifically because one side is uuid-family and the other is a
        # compatible plain string (e.g. Snowflake VARCHAR, which has no
        # native uuid type), force BOTH sides to render via UUID-1 — the
        # native-uuid side already resolves to UUID-1 on its own, but the
        # override makes that explicit and, critically, is what makes the
        # text-stored side render lower-case/hyphenated too, rather than
        # falling through to its own native STR-1/STR-2.
        if (_family(src_rule) == "uuid") != (_family(tgt_rule) == "uuid"):
            rule_overrides[name] = NormalisationRule.UUID_1

        # DEC-1: canonical scale is the MIN of both sides' declared scale
        # (spec §6.1/§6.2) — never trust either side alone.
        if src_rule is NormalisationRule.DEC_1 or tgt_rule is NormalisationRule.DEC_1:
            src_scale = src_col.scale if src_col.scale is not None else 0
            tgt_scale = tgt_col.scale if tgt_col.scale is not None else 0
            p = min(src_scale, tgt_scale)
            scale_overrides[name] = p
            if src_scale != tgt_scale:
                warnings.append(
                    TdWarning(
                        f"DEC-1: column {name!r} scale differs "
                        f"(source={src_scale} target={tgt_scale}) — comparing at scale {p}",
                        rule=NormalisationRule.DEC_1,
                        column=name,
                    )
                )

        # TS-2: a timestamp/time pair whose fractional-second precision
        # differs is upgraded from TS-1/TS-3/TIME-1 to TS-2, ROUNDED
        # half-up to the LOWER of the two precisions (spec clarification:
        # not truncated — confirmed against Postgres's own typmod
        # rounding, e.g. '23:59:59.999999'::timestamptz(0) carries into
        # the next day rather than dropping the fraction). The same rule
        # ID applies to a `time`-only pair, not just timestamps — a
        # deliberate reuse rather than a new TIME-2 rule. Default to 6
        # (Postgres' own microsecond default) when a side doesn't report
        # a precision.
        if _family(src_rule) in ("timestamp", "time") or _family(tgt_rule) in ("timestamp", "time"):
            src_p = src_col.precision if src_col.precision is not None else 6
            tgt_p = tgt_col.precision if tgt_col.precision is not None else 6
            if src_p != tgt_p:
                p = min(src_p, tgt_p)
                precision_overrides[name] = p
                rule_overrides[name] = NormalisationRule.TS_2
                warnings.append(
                    TdWarning(
                        f"TS-2: column {name!r} precision differs "
                        f"(source={src_p} target={tgt_p}) — comparing at precision {p}",
                        rule=NormalisationRule.TS_2,
                        column=name,
                    )
                )

        # STR-2: --trim/--case-insensitive (spec §6.1) promote STR-1 to
        # STR-2 for *every* text column when either flag is on — it's a
        # global CLI opt-in, not a per-column schema fact.
        if src_rule is NormalisationRule.STR_1 and (base.trim or base.case_insensitive):
            rule_overrides[name] = NormalisationRule.STR_2

    options = NormaliseOptions(
        trim=base.trim,
        case_insensitive=base.case_insensitive,
        float_precision=base.float_precision,
        assume_tz=base.assume_tz,
        scale_overrides=scale_overrides,
        precision_overrides=precision_overrides,
    )
    return ColumnMatch(
        matched=matched,
        excluded=excluded,
        rule_overrides=rule_overrides,
        options=options,
        warnings=warnings,
    )
