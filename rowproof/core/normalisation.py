"""Rule selection: which §6.1 canonical form applies to a given native type.

This module only *picks* the rule (a pure function of the native type
string). Rendering the rule as engine SQL is the connector's job
(`Connector.normalise_expr`) — spec §5 keeps engine-specific SQL out of
core/ entirely.
"""

from __future__ import annotations

from rowproof.core.models import NormalisationRule

_INT_TYPES = {
    "smallint",
    "integer",
    "int",
    "int2",
    "int4",
    "int8",
    "bigint",
    "serial",
    "smallserial",
    "bigserial",
}

_TEXT_PREFIXES = (
    "character varying",
    "varchar",
    "character",
    "char",
)

_TEXT_TYPES = {
    "text",
    "name",
    "citext",
    "bpchar",
}

_DECIMAL_TYPES = {"numeric", "decimal"}
_FLOAT_TYPES = {
    "real", "double precision", "float4", "float8", "float",
}
_BOOL_TYPES = {"boolean", "bool"}
_DATE_TYPES = {"date"}
_TIME_PREFIXES = ("time",)  # "time", "time with time zone", "time(6)", ...
_BINARY_TYPES = {"bytea"}
_JSON_TYPES = {"json", "jsonb"}

_TIMESTAMP_PREFIXES = ("timestamp",)


def pick_rule(native_type: str) -> NormalisationRule:
    """Return the NormalisationRule that governs comparison of this type.

    Matching is case-insensitive and tolerant of parameterised types
    (`varchar(50)`, `numeric(12,2)`) since a connector's get_schema() may
    report either the bare type name or the full declared type.

    This picks the rule from a *single* column's own native type. Two
    rules in §6.1 — DEC-1 (min scale of the *pair*) and TS-2 (timestamp
    precision differs between the pair) — can only be decided once both
    sides' schemas are known, so a column that *this* function maps to
    TS_1/TS_3 may still be *upgraded* to TS_2 by the column-matching step
    (§6.2) when the paired column's precision differs; DEC-1 itself is
    always picked here (a decimal column is DEC-1 on both sides
    regardless of the pair), but the *rendering* precision (`p`) still
    comes from the pair via `NormaliseOptions.scale_overrides`.
    """
    t = native_type.strip().lower()

    if t in _INT_TYPES:
        return NormalisationRule.INT_1

    if t in _DECIMAL_TYPES or t.startswith("numeric(") or t.startswith("decimal("):
        return NormalisationRule.DEC_1

    if t in _FLOAT_TYPES:
        return NormalisationRule.FLT_1

    if t in _BOOL_TYPES:
        return NormalisationRule.BOOL_1

    if t in _DATE_TYPES:
        return NormalisationRule.DATE_1

    if t.startswith(_TIME_PREFIXES) and not t.startswith(_TIMESTAMP_PREFIXES):
        return NormalisationRule.TIME_1

    if t.startswith(_TIMESTAMP_PREFIXES):
        if "with time zone" in t or t == "timestamptz":
            return NormalisationRule.TS_1
        return NormalisationRule.TS_3

    if t == "uuid":
        return NormalisationRule.UUID_1

    if t in _BINARY_TYPES:
        return NormalisationRule.BIN_1

    if t in _JSON_TYPES:
        return NormalisationRule.JSON_1

    if t == "enum":
        # Connector sentinel (Postgres reports enums as data_type
        # "USER-DEFINED" — PostgresConnector.get_schema() preprocesses
        # that into this "enum" string before pick_rule ever sees it).
        return NormalisationRule.ENUM_1

    if t.endswith("[]"):
        # Connector sentinel for array columns (Postgres reports these as
        # data_type "ARRAY" with the element type in udt_name —
        # PostgresConnector.get_schema() reconstructs "<elem>[]").
        return NormalisationRule.ARR_1

    if t in _TEXT_TYPES or t.startswith(_TEXT_PREFIXES):
        return NormalisationRule.STR_1

    return NormalisationRule.UNK_1
