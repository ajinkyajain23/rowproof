"""Postgres connector (spec §5). See _pgwire.py for *how* it talks to
Postgres — this class's public shape is exactly the `Connector` protocol
regardless.
"""

from __future__ import annotations

from typing import Callable

import psycopg

from tablediff.connectors import _pgwire
from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, TableRef

_INT_TYPES = {
    "smallint", "integer", "bigint", "int2", "int4", "int8",
    "smallserial", "serial", "bigserial",
}

# A `collation` this connector reports as already byte-order-safe —
# Postgres's own "C" and "POSIX", or the libc "C.UTF-8" locale a plain
# `initdb` sometimes defaults to. See `key_order_expr`'s docstring.
_BYTE_ORDER_SAFE_COLLATIONS = {"c", "posix", "c.utf8", "c.utf-8", "ucs_basic"}


class PostgresConnector:
    engine = "postgres"

    def __init__(self) -> None:
        # One persistent connection for this side's whole lifetime (spec
        # §5: "One connection per side. No connection pooling in v1.") —
        # not a fresh connection per query, which measured ~17ms of pure
        # reconnect overhead against a local Postgres and would blow the
        # 100M-row/5-minute benchmark (spec §13 M2) several times over on
        # its own across the thousands of per-segment queries a large
        # diff issues.
        self._conn: psycopg.Connection | None = None
        # Set by the CLI under --verbose (spec §5: "Every generated SQL
        # statement is logged at --verbose ... This is a feature.").
        self.on_query: Callable[[str], None] | None = None

    def connect(self, dsn: str) -> None:
        parsed = _pgwire.parse_pg_dsn(dsn)
        self._conn = _pgwire.open_connection(parsed)

    def close(self) -> None:
        if self._conn is not None:
            _pgwire.close_connection(self._conn)
            self._conn = None

    def _require_conn(self) -> psycopg.Connection:
        if self._conn is None:
            raise RuntimeError("connect() must be called before use")
        return self._conn

    def query(self, sql: str, params: dict | None = None) -> list[tuple]:
        if self.on_query is not None:
            self.on_query(sql)
        return _pgwire.run_query_on(self._require_conn(), sql)

    def get_schema(self, table: TableRef) -> list[Column]:
        schema = table.schema or "public"
        sql = (
            "SELECT c.column_name, c.data_type, c.is_nullable, c.ordinal_position, "
            # information_schema.columns.collation_name is NULL whenever a
            # column just uses the database's default collation (it's only
            # populated for an *explicit* non-default COLLATE on the
            # column) — coalesce down to the database's actual default so
            # Column.collation always reflects the collation really in
            # effect, never a false "unknown". core/hashdiff.py depends on
            # this being the *effective* collation to safely skip forcing
            # COLLATE "C" (and keep a PK index usable) whenever the
            # column's real collation is already byte-order-safe. This
            # also reports a (meaningless but harmless) collation for
            # non-collatable types like uuid or int — core/hashdiff.py
            # always checks native_type before ever consulting collation,
            # so that value is simply never read for those columns.
            "COALESCE(c.collation_name, (SELECT datcollate FROM pg_database WHERE datname = current_database())), "
            # DEC-1 (§6.1): NULL for non-numeric columns.
            "c.numeric_scale, "
            # TS-2 (§6.1): NULL for non-temporal columns. Postgres reports
            # this for timestamp/time/interval columns (6 by default,
            # fewer when the column was declared e.g. timestamp(3)).
            "c.datetime_precision, "
            # udt_name is what lets us tell an ARRAY column's *element*
            # type apart (data_type alone just says the literal string
            # "ARRAY" for every array, regardless of element type) — for
            # an array column Postgres reports udt_name as "_" + the
            # element's own udt_name (e.g. "_int4", "_text").
            "c.udt_name, "
            # typtype = 'e' identifies an enum type in pg_type — that's
            # the only reliable way to tell "USER-DEFINED" enum columns
            # apart from other user-defined types (domains, composites)
            # information_schema also reports as "USER-DEFINED".
            "(SELECT t.typtype FROM pg_catalog.pg_type t WHERE t.typname = c.udt_name) "
            "FROM information_schema.columns c "
            f"WHERE c.table_schema = {self.quote_literal(schema)} "
            f"AND c.table_name = {self.quote_literal(table.table)} "
            "ORDER BY c.ordinal_position"
        )
        rows = self.query(sql)
        columns = []
        for name, data_type, is_nullable, ordinal, collation_name, scale, precision, udt_name, typtype in rows:
            native_type = self._resolve_native_type(data_type, udt_name, typtype)
            columns.append(
                Column(
                    name=name,
                    native_type=native_type,
                    nullable=(is_nullable == "YES"),
                    ordinal=int(ordinal),
                    # NULL for non-collatable types (int, uuid, ...); the
                    # column's actual declared/default collation name for
                    # text-like types (e.g. "C", "en-x-icu") otherwise. See
                    # this class's own key_order_expr() for why this
                    # matters for segmentation performance.
                    collation=collation_name,
                    scale=(int(scale) if scale is not None else None),
                    precision=(int(precision) if precision is not None else None),
                )
            )
        return columns

    @staticmethod
    def _resolve_native_type(data_type: str, udt_name: str, typtype: str | None) -> str:
        """Turn information_schema's generic `data_type` (which just says
        "ARRAY" or "USER-DEFINED" for anything beyond the built-in scalar
        types, with no further detail) into the specific string
        core.normalisation.pick_rule() knows how to route: "<elem>[]" for
        arrays (ARR-1) and the sentinel "enum" for enum types (ENUM-1).
        Anything else (domains, composites, ranges, ...) is passed through
        as-is, which pick_rule() safely falls back to UNK-1 for — spec
        §6.1's "never fail a run because of an unknown type" applies here
        too; M1 only commits to arrays and enums by name.
        """
        if data_type == "ARRAY":
            elem = udt_name[1:] if udt_name.startswith("_") else udt_name
            return f"{elem}[]"
        if data_type == "USER-DEFINED" and typtype == "e":
            return "enum"
        return data_type

    def get_primary_key(self, table: TableRef) -> list[str] | None:
        schema = table.schema or "public"
        sql = (
            "SELECT kcu.column_name "
            "FROM information_schema.table_constraints tc "
            "JOIN information_schema.key_column_usage kcu "
            "  ON tc.constraint_name = kcu.constraint_name "
            "  AND tc.table_schema = kcu.table_schema "
            "WHERE tc.constraint_type = 'PRIMARY KEY' "
            f"AND tc.table_schema = {self.quote_literal(schema)} "
            f"AND tc.table_name = {self.quote_literal(table.table)} "
            "ORDER BY kcu.ordinal_position"
        )
        rows = self.query(sql)
        if not rows:
            return None
        return [r[0] for r in rows]

    def normalise_expr(
        self,
        column: Column,
        rule: NormalisationRule,
        options: NormaliseOptions | None = None,
    ) -> str:
        quoted = self.quote_identifier(column.name)
        native = column.native_type.lower()
        opts = options or NormaliseOptions()

        if rule is NormalisationRule.INT_1:
            inner = f"({quoted}::text)"
        elif rule is NormalisationRule.DEC_1:
            p = opts.scale_overrides.get(column.name, column.scale if column.scale is not None else 0)
            inner = self._dec1_expr(quoted, p)
        elif rule is NormalisationRule.FLT_1:
            inner = self._flt1_expr(quoted, opts.float_precision)
        elif rule in (NormalisationRule.TS_1, NormalisationRule.TS_3):
            # TS-1 (tz-aware) always converts to UTC first; TS-3 (naive)
            # has no tz to convert — spec: "rendered as-is ... assumed
            # UTC". Both always render the full 6 fractional digits (spec
            # TS-1: "Always 6 fractional digits"; TS-3 inherits the same
            # rendering, just without the timezone() conversion).
            if "with time zone" in native or native == "timestamptz":
                inner = (
                    f"to_char(timezone('UTC', {quoted}), "
                    "'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
                )
            else:
                inner = f"to_char({quoted}, 'YYYY-MM-DD\"T\"HH24:MI:SS.US\"Z\"')"
        elif rule is NormalisationRule.TS_2:
            p = opts.precision_overrides.get(column.name, column.precision if column.precision is not None else 6)
            inner = self._ts2_expr(quoted, native, p)
        elif rule is NormalisationRule.DATE_1:
            inner = f"to_char({quoted}, 'YYYY-MM-DD')"
        elif rule is NormalisationRule.TIME_1:
            inner = f"to_char({quoted}, 'HH24:MI:SS.US')"
        elif rule is NormalisationRule.BOOL_1:
            inner = f"(CASE WHEN {quoted} THEN 'true' ELSE 'false' END)"
        elif rule is NormalisationRule.UUID_1:
            # Postgres already renders its native uuid type as lower-case,
            # hyphenated text — the canonical UUID-1 form is a plain cast.
            inner = f"({quoted}::text)"
        elif rule is NormalisationRule.BIN_1:
            inner = f"encode({quoted}, 'hex')"
        elif rule is NormalisationRule.JSON_1:
            # jsonb's own storage format canonicalises key order and
            # whitespace (spec §6.1: "compare as text after engine-side
            # canonicalisation if available"). Casting a plain `json`
            # column through jsonb picks up that same canonicalisation;
            # a jsonb column's own ::jsonb cast is a no-op.
            inner = f"({quoted}::jsonb::text)"
        elif rule is NormalisationRule.ENUM_1:
            inner = f"({quoted}::text)"
        elif rule is NormalisationRule.ARR_1:
            inner = self._arr1_expr(quoted)
        elif rule is NormalisationRule.STR_2:
            inner = self._str2_expr(quoted, opts)
        else:  # STR_1, UNK_1 fallback
            inner = f"({quoted}::text)"

        if column.nullable:
            return f"(CASE WHEN {quoted} IS NULL THEN E'\\\\N' ELSE {inner} END)"
        return inner

    @staticmethod
    def _dec1_expr(quoted: str, p: int) -> str:
        """DEC-1 (§6.1): digits with exactly `p` decimal places, round
        half-even ("banker's rounding") — NOT Postgres's own `round()`,
        which rounds half away from zero (empirically confirmed: round
        both directions from the same value differ at exact .5 scaled
        midpoints). Exact because every step stays in `numeric` (no float
        involved), so the `= 0.5` comparison at the scaled midpoint is
        exact rather than an epsilon guess.
        """
        p = max(p, 0)
        pow10 = f"power(10::numeric, {p})"
        scaled = f"(({quoted})::numeric * {pow10})"
        floor_expr = f"floor({scaled})"
        # Round-half-even: at an exact .5 midpoint, round to whichever of
        # floor/floor+1 is even; Postgres's round() already does the
        # right thing (round to nearest, ties away from zero == ties
        # towards the further integer) for every OTHER case, so only the
        # exact-half case needs the override.
        banker = (
            f"(CASE WHEN ({scaled} - {floor_expr}) = 0.5 "
            f"THEN (CASE WHEN {floor_expr}::numeric % 2 = 0 THEN {floor_expr} ELSE {floor_expr} + 1 END) "
            f"ELSE round({scaled}) END)"
        )
        rounded_value = f"({banker} / {pow10})"
        fmt = "FM999999999999999990" if p == 0 else "FM999999999999999990." + ("0" * p)
        return f"to_char({rounded_value}, '{fmt}')"

    @staticmethod
    def _flt1_expr(quoted: str, sig_digits: int) -> str:
        """FLT-1 (§6.1): scientific notation, `sig_digits` significant
        digits (default 15, `--float-precision N` overrides). NaN/
        Infinity/-Infinity must be special-cased BEFORE to_char's
        scientific-notation format, which renders garbage
        ("#.##################") for all three (empirically confirmed).
        to_char also reserves a leading space for a positive number's
        sign, hence the trim().
        """
        sig_digits = max(sig_digits, 1)
        if sig_digits == 1:
            fmt = "9EEEE"
        else:
            fmt = "9." + ("9" * (sig_digits - 1)) + "EEEE"
        return (
            "(CASE "
            f"WHEN {quoted} = 'NaN'::float8 THEN 'NaN' "
            f"WHEN {quoted} = 'Infinity'::float8 THEN 'Infinity' "
            f"WHEN {quoted} = '-Infinity'::float8 THEN '-Infinity' "
            f"ELSE trim(to_char({quoted}, '{fmt}')) END)"
        )

    @staticmethod
    def _ts2_expr(quoted: str, native: str, p: int) -> str:
        """TS-2 (§6.1, clarified): both sides ROUNDED half-up to the
        LOWER of the pair's fractional-second precision — NOT truncated.
        Empirically confirmed this is what Postgres's own typmod rounding
        does (`'23:59:59.999999'::timestamptz(0)` carries into the next
        day; `'13:45:07.9995'::time(3)` carries into the next second), so
        TS-2 has to agree or a genuinely-identical value falsely reports
        as changed right at that boundary.

        Rounding a timestamp/time correctly means the SQL engine has to
        handle the carry (seconds -> minutes -> ... -> days) — string
        formatting can't do that safely. `date_bin(stride, ts, origin)`
        floors `ts` to the nearest multiple of `stride` since `origin`;
        adding half a stride first before binning is the standard
        floor-to-round trick, and turns this into exact round-half-up
        (ties away from zero, matching Postgres's own typmod behaviour —
        confirmed above) using real timestamp arithmetic instead of text.

        Applies to plain `time` columns too (spec clarification: TS-2 is
        reused for time-precision pairs, not a separate rule) — a `time`
        has no date component for `date_bin` to work with, so it's first
        anchored onto a fixed date, rounded there, and only the
        HH:MI:SS[.ffffff] portion is rendered back out; rounding past
        23:59:59 on that fixed date correctly wraps to 00:00:00, matching
        how Postgres's own `time` typmod rounding wraps at midnight.
        """
        p = max(0, min(p, 6))
        stride = "interval '1 second'" if p == 0 else f"(interval '1 second' / {10 ** p})"
        is_time_only = native.startswith("time") and not native.startswith("timestamp")
        if is_time_only:
            base_time = f"({quoted} AT TIME ZONE 'UTC')" if "with time zone" in native else quoted
            base = f"(DATE '2000-01-01' + {base_time})"
        elif "with time zone" in native or native == "timestamptz":
            base = f"timezone('UTC', {quoted})"
        else:
            base = quoted
        rounded = f"date_bin({stride}, {base} + {stride} / 2, timestamp '2000-01-01')"
        frac = f'.FF{p}' if p > 0 else ""
        if is_time_only:
            fmt = f'HH24:MI:SS{frac}'
        else:
            fmt = f'YYYY-MM-DD"T"HH24:MI:SS{frac}"Z"'
        return f"to_char({rounded}, '{fmt}')"

    @staticmethod
    def _arr1_expr(quoted: str) -> str:
        """ARR-1 (§6.1): `[` + elements in canonical form joined by `,` +
        `]`, order-sensitive. Element-level canonicalisation here is a
        plain `::text` cast (correct for the common cases — int/text/
        bool/uuid arrays) rather than recursively applying each element's
        own §6.1 rule (e.g. DEC-1 rounding inside a `numeric[]`) — a
        documented scope limit for M1, not silently wrong: array-of-
        decimal/timestamp columns just compare at full native precision
        per element instead of the pair's negotiated scale. NULL elements
        render as `\\N`, matching NULL-1; NULL vs a genuinely empty array
        stay distinct ('[]' vs the outer NULL-1 wrap) per the §6.4 fixture.
        """
        return (
            "('[' || COALESCE((SELECT string_agg("
            "CASE WHEN elem IS NULL THEN E'\\\\N' ELSE elem::text END, ',' ORDER BY ord"
            f") FROM unnest({quoted}) WITH ORDINALITY AS t(elem, ord)), '') || ']')"
        )

    @staticmethod
    def _str2_expr(quoted: str, opts: NormaliseOptions) -> str:
        """STR-2 (§6.1), opt-in: `--trim` strips trailing whitespace,
        `--case-insensitive` lower-cases. Both may be on at once."""
        expr = quoted
        if opts.trim:
            expr = f"rtrim({expr})"
        if opts.case_insensitive:
            expr = f"lower({expr})"
        return f"({expr}::text)"

    def row_hash_expr(self, exprs: list[str]) -> str:
        # spec §5: "Postgres via ('x' || substr(md5(...), 1, 16))::bit(64)::bigint"
        concat = " || E'\\x1f' || ".join(exprs) if len(exprs) > 1 else exprs[0]
        return f"(('x' || substr(md5({concat}), 1, 16))::bit(64)::bigint)"

    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        # Order-independent: SUM is commutative. Cast to numeric first so
        # summing many bigints can't overflow bigint's own range, then fold
        # back into an unsigned 64-bit space with a double modulo (handles
        # the sum coming out negative).
        modulus = "18446744073709551616"  # 2^64
        return (
            f"((sum(({row_hash_expr})::numeric) % {modulus} + {modulus}) % {modulus})"
        )

    def key_order_expr(self, column: Column, numeric: bool) -> str:
        """Left to a query's own default, a Postgres database's *default*
        collation is usually locale-aware (case- and sometimes
        punctuation-sensitive ordering), not a plain byte compare. If the
        sample used to pick quantile boundaries sorted one way while a
        segment's `WHERE lo <= key < hi` compared another way, a key
        could satisfy neither boundary (silently dropped from every
        segment) or both of two adjacent ones (double-counted) — exactly
        the kind of gap that lets a real difference vanish for
        mixed-case or non-ASCII text keys. Numeric keys never call this
        (arithmetic bounds are used for those, not a sampled sort order,
        so no such mismatch is possible there).

        Three shapes:

        * `uuid` — always the RAW column. uuid has its own native btree
          opclass and isn't a collatable type at all (`COLLATE` on a
          uuid expression is a Postgres error, not a no-op), so there is
          no collation ambiguity to resolve in the first place.
        * a `collation` this connector reports as already byte-order-safe
          (`_BYTE_ORDER_SAFE_COLLATIONS`) — also the RAW column. Forcing
          `COLLATE "C"` here would be correct but pointless: it's
          provably the same order the column (and its PK index) already
          use, so comparing the raw column keeps the index usable while
          losing nothing.
        * anything else (an unrecognised or locale-aware collation — e.g.
          a typical production `en_US.utf8` default, or `en-x-icu` — and
          a defensive fallback when `collation is None`) — `col COLLATE
          "C"`, forced explicitly. This is the one case where a real,
          unavoidable trade-off exists: guaranteeing one deterministic
          order across bounds/sample/segment queries takes priority over
          the index, so this accepts a sequential scan rather than risk
          the silent-data-loss bug `core.segmentation.clamp_to_range`/
          `assert_contiguous_coverage` exist to prevent. Confirmed both
          ways with EXPLAIN against real Postgres: a "C"/"C.UTF-8"-default
          column keeps its Index Scan; an `en_US.utf8` or `en-x-icu` one
          falls back to a Seq Scan.
        """
        q = self.quote_identifier(column.name)
        if numeric:
            return q
        if column.native_type.strip().lower() == "uuid":
            return q
        if column.collation is not None and column.collation.strip().lower() in _BYTE_ORDER_SAFE_COLLATIONS:
            return q
        return f'{q} COLLATE "C"'

    def key_bounds_expr(self, column: Column, numeric: bool) -> str:
        """uuid needs one further exception here: Postgres has no
        `MIN`/`MAX` *aggregate* registered for the uuid type at all
        (confirmed against a real instance — `SELECT MIN(uuid_col)`
        fails with "function min(uuid) does not exist"; this is despite
        uuid having full ordering operators, which is exactly why the
        raw column works fine as a *comparison* target in
        `key_order_expr`'s segment predicates). So the bounds query
        alone still needs the CAST-to-TEXT workaround — MIN/MAX(text) is
        a real aggregate.

        That cast's ordering must still agree with the raw-column
        ordering `key_order_expr` uses everywhere else, or the "true
        min" this produces could disagree with what the segment
        predicates actually cover (the same class of bug clamp_to_range
        exists to prevent). Verified directly against Postgres: canonical
        lowercase uuid text is fixed-width with hyphens at fixed
        positions, so byte-order (`COLLATE "C"`) comparison of the text
        form exactly matches uuid's own native byte comparison —
        `ORDER BY v` and `ORDER BY v::text COLLATE "C"` produced
        identical orderings for a mixed-case sample. This is the one
        place that cast is added back; everywhere else uses the raw
        column so segment queries can still use the PK index. Bounds
        runs once per side, not once per segment, so this one text cast
        is not on the hot path segmentation cares about.
        """
        if not numeric and column.native_type.strip().lower() == "uuid":
            q = self.quote_identifier(column.name)
            return f'CAST({q} AS TEXT) COLLATE "C"'
        return self.key_order_expr(column, numeric)

    def key_select_expr(self, column: Column) -> str:
        # Postgres's OUTER JOIN already produces genuine NULL for an
        # unmatched row regardless of the column's own NOT NULL
        # constraint (a constraint on stored rows, not on join results —
        # confirmed against a real server), so the raw column is always
        # correct here.
        return self.quote_identifier(column.name)

    def sample_sql(self, table_sql: str, key_col: str, sample_cap: int) -> str:
        q = self.quote_identifier(key_col)
        return f"SELECT {q} FROM {table_sql} ORDER BY RANDOM() LIMIT {sample_cap}"

    def quote_identifier(self, name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def quote_literal(self, value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("'", "''")
        if "\\" in escaped:
            escaped = escaped.replace("\\", "\\\\")
            return f"E'{escaped}'"
        return f"'{escaped}'"
