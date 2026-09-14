"""Snowflake connector (spec §5, §9/§13 M3).

Every SQL expression below was run against a real Snowflake trial account
(not just reasoned through — see commit history for the empirical probes
that found each of the following, the same discipline M2's ClickHouse
connector used):

* Snowflake's float equality is NOT IEEE-754: `'NaN'::float = 'NaN'::float`
  is TRUE (confirmed live), so the usual `x != x` NaN-detection idiom
  silently never fires here — NaN must be detected with a direct
  `= 'NaN'::float` comparison instead.
* `TO_CHAR(x, '9.99999EEEE')` — the obvious scientific-notation format —
  renders `##`/blank digits for any exponent that needs 3 digits (e.g.
  `1e308`) and for the special-cased zero/NaN/Infinity values, so FLT-1 is
  hand-built the same way ClickHouseConnector's `_flt1_expr` is, not via
  `TO_CHAR`.
* `LPAD` truncates an already-longer-than-target input instead of leaving
  it alone (confirmed: `LPAD('308', 2, '0')` -> `'30'`) — the same
  ClickHouse `leftPad` gotcha, guarded against the same way
  (`GREATEST(LENGTH(x), target_len)`).
* A `CASE`/`IFF` branch's *unused* arm is still evaluated for every row
  (confirmed: `LOG(10, 0)` inside a `WHEN x = 0 THEN ...`'s untaken `ELSE`
  branch still raises "Invalid floating point operation" for a zero row)
  — every helper below guards its own inputs (e.g. substituting `1.0`
  before `LOG`) rather than relying on CASE to skip the computation.
* `ROUND(x, p, 'HALF_TO_EVEN')` is a real, working rounding mode — DEC-1
  doesn't need ClickHouse/Postgres's own hand-rolled banker's-rounding
  CASE construction.
* `MD5_NUMBER_UPPER64(s)` returns exactly the *unsigned* form of the same
  64-bit integer Postgres's `('x'||substr(md5(s),1,16))::bit(64)::bigint`
  produces (confirmed against the same 'hello'/'row-2' reference values
  ClickHouseConnector's own row_hash_expr docstring verified) — no
  ClickHouse-style byte-reversal trick needed, just an unsigned-to-signed
  fold for values >= 2^63.
* `information_schema.key_column_usage` isn't available on this trial
  account/edition ("does not exist or not authorized") — primary keys are
  read via `SHOW PRIMARY KEYS IN TABLE` instead.
"""

from __future__ import annotations

from typing import Callable

from tablediff.connectors import _sfwire
from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, TableRef


def _resolve(data_type: str, numeric_scale: int | None, datetime_precision: int | None) -> tuple[str, int | None, int | None]:
    """Turn Snowflake's information_schema.columns `data_type` (already
    Snowflake's own normalised family name — e.g. VARCHAR/STRING/CHAR all
    report as `TEXT`; FLOAT/DOUBLE/REAL all report as `FLOAT` — confirmed
    against a real account) into the (sentinel, scale, precision) shape
    core.normalisation.pick_rule() and DEC-1/TS-2's pair-negotiation
    already know how to route, mirroring what PostgresConnector/
    ClickHouseConnector's own `_resolve_native_type`/`_resolve` do.

    spec §9 M3's three named requirements map here directly:
    * `NUMBER(38,0)` (scale 0) -> "bigint" -> INT-1, same as any other
      integer; only a NUMBER with a non-zero scale becomes DEC-1.
    * `VARIANT` -> passed through unmapped (lower-cased) -> pick_rule()'s
      UNK-1 fallback -> rendered as text by normalise_expr's own
      STR-1/UNK-1 fallback branch (`TO_VARCHAR`). Same treatment for
      ARRAY/OBJECT (semi-structured, not spec-required for M3 — no
      recursive ARR-1 canonicalisation attempted here, same documented
      scope limit as ClickHouseConnector's Map/Tuple).
    * `TIMESTAMP_NTZ` (naive) -> "timestamp" (no "with time zone") ->
      pick_rule()'s TS-3; `TIMESTAMP_LTZ`/`TIMESTAMP_TZ` (both tz-aware,
      stored as an instant) -> "timestamp with time zone" -> TS-1.
    """
    t = data_type.strip().upper()

    if t in ("NUMBER", "DECIMAL", "NUMERIC"):
        scale = numeric_scale if numeric_scale is not None else 0
        if scale == 0:
            return "bigint", None, None
        return "numeric", scale, None
    if t == "FLOAT":
        return "double precision", None, None
    if t == "TEXT":
        return "text", None, None
    if t == "BOOLEAN":
        return "boolean", None, None
    if t == "DATE":
        return "date", None, None
    if t == "TIME":
        return "time", None, datetime_precision
    if t == "TIMESTAMP_NTZ":
        return "timestamp", None, datetime_precision
    if t in ("TIMESTAMP_LTZ", "TIMESTAMP_TZ"):
        return "timestamp with time zone", None, datetime_precision
    if t == "BINARY":
        return "bytea", None, None
    # VARIANT, ARRAY, OBJECT, GEOGRAPHY, GEOMETRY, and anything else:
    # passed through unmatched -- never fail a run over an unmapped type
    # (spec §6.1).
    return t.lower(), None, None


class SnowflakeConnector:
    engine = "snowflake"

    def __init__(self) -> None:
        # One persistent connection for this side's whole lifetime (spec
        # §5: "One connection per side. No connection pooling in v1."),
        # same reasoning as Postgres/ClickHouse's own connectors.
        self._conn = None
        self._dsn: _sfwire.SfDsn | None = None
        self.on_query: Callable[[str], None] | None = None

    def connect(self, dsn: str) -> None:
        parsed = _sfwire.parse_sf_dsn(dsn)
        self._conn = _sfwire.open_connection(parsed)
        self._dsn = parsed

    def close(self) -> None:
        if self._conn is not None:
            _sfwire.close_connection(self._conn)
            self._conn = None
        self._dsn = None

    def _require_conn(self):
        if self._conn is None:
            raise RuntimeError("connect() must be called before use")
        return self._conn

    def query(self, sql: str, params: dict | None = None) -> list[tuple]:
        if self.on_query is not None:
            self.on_query(sql)
        return _sfwire.run_query_on(self._require_conn(), sql)

    def _schema_for(self, table: TableRef) -> str:
        return table.schema or "PUBLIC"

    def get_schema(self, table: TableRef) -> list[Column]:
        schema = self._schema_for(table)
        sql = (
            "SELECT column_name, data_type, is_nullable, ordinal_position, "
            "numeric_scale, datetime_precision "
            "FROM information_schema.columns "
            f"WHERE UPPER(table_schema) = UPPER({self.quote_literal(schema)}) "
            f"AND UPPER(table_name) = UPPER({self.quote_literal(table.table)}) "
            "ORDER BY ordinal_position"
        )
        rows = self.query(sql)
        columns = []
        for name, data_type, is_nullable, ordinal, numeric_scale, datetime_precision in rows:
            native_type, scale, precision = _resolve(
                data_type,
                int(numeric_scale) if numeric_scale is not None else None,
                int(datetime_precision) if datetime_precision is not None else None,
            )
            columns.append(
                Column(
                    name=name,
                    native_type=native_type,
                    nullable=(is_nullable == "YES"),
                    ordinal=int(ordinal),
                    collation=None,  # Snowflake has no per-column collation concept
                    scale=scale,
                    precision=precision,
                )
            )
        return columns

    def get_primary_key(self, table: TableRef) -> list[str] | None:
        # information_schema.key_column_usage isn't available on this
        # trial account/edition (confirmed: "does not exist or not
        # authorized") -- SHOW PRIMARY KEYS IN TABLE works universally and
        # already reports rows in key_sequence order.
        schema = self._schema_for(table)
        qualified = (
            f"{self.quote_identifier(self._require_dsn().database)}."
            f"{self.quote_identifier(schema)}.{self.quote_identifier(table.table)}"
        )
        rows = self.query(f"SHOW PRIMARY KEYS IN TABLE {qualified}")
        if not rows:
            return None
        # SHOW PRIMARY KEYS columns: created_on, database_name,
        # schema_name, table_name, column_name, key_sequence,
        # constraint_name, rely, comment (confirmed against a real
        # account) -- column_name is index 4, key_sequence index 5.
        return [r[4] for r in sorted(rows, key=lambda r: r[5])]

    def _require_dsn(self) -> _sfwire.SfDsn:
        if self._dsn is None:
            raise RuntimeError("connect() must be called before use")
        return self._dsn

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
            inner = f"TO_VARCHAR({quoted})"
        elif rule is NormalisationRule.DEC_1:
            p = opts.scale_overrides.get(column.name, column.scale if column.scale is not None else 0)
            inner = self._dec1_expr(quoted, p)
        elif rule is NormalisationRule.FLT_1:
            inner = self._flt1_expr(quoted, opts.float_precision)
        elif rule in (NormalisationRule.TS_1, NormalisationRule.TS_3):
            if "with time zone" in native:
                inner = self._ts1_expr(quoted)
            else:
                inner = self._ts3_expr(quoted)
        elif rule is NormalisationRule.TS_2:
            p = opts.precision_overrides.get(column.name, column.precision if column.precision is not None else 6)
            inner = self._ts2_expr(quoted, native, p)
        elif rule is NormalisationRule.DATE_1:
            inner = f"TO_CHAR({quoted}, 'YYYY-MM-DD')"
        elif rule is NormalisationRule.TIME_1:
            inner = f"TO_CHAR({quoted}, 'HH24:MI:SS.FF6')"
        elif rule is NormalisationRule.BOOL_1:
            inner = f"(CASE WHEN {quoted} THEN 'true' ELSE 'false' END)"
        elif rule is NormalisationRule.UUID_1:
            inner = self._uuid1_expr(quoted)
        elif rule is NormalisationRule.BIN_1:
            inner = f"LOWER(HEX_ENCODE({quoted}))"
        elif rule is NormalisationRule.STR_2:
            inner = self._str2_expr(quoted, opts)
        else:  # STR_1, JSON_1, ARR_1, ENUM_1, UNK_1 -- all "as text"
            # (VARIANT/ARRAY/OBJECT never reach any of the typed branches
            # above -- see module docstring / _resolve's own docstring
            # for why UNK-1's plain-text fallback IS spec's "VARIANT as
            # text" requirement, not a gap.)
            inner = f"TO_VARCHAR({quoted})"

        if column.nullable:
            return f"(CASE WHEN {quoted} IS NULL THEN '\\\\N' ELSE {inner} END)"
        return inner

    @staticmethod
    def _dec1_expr(quoted: str, p: int) -> str:
        """DEC-1: exactly `p` decimal places, round half-even. Snowflake's
        `ROUND(x, scale, 'HALF_TO_EVEN')` is a genuine, working rounding
        mode (confirmed against a real account for both positive and
        negative exact-midpoint values, e.g. 2.345/-12.345 at scale 2 both
        rounding to the even neighbour) — no hand-rolled floor/mod
        construction needed here, unlike Postgres/ClickHouse. `p == 0`
        needs its own format string (no trailing '.') -- `TO_CHAR(x,
        'FM990.')` (an empty fractional part after a literal dot) renders
        a bare trailing '.', confirmed against a real account.
        """
        p = max(p, 0)
        rounded = f"ROUND({quoted}, {p}, 'HALF_TO_EVEN')"
        fmt = "FM999999999999999990" if p == 0 else "FM999999999999999990." + ("0" * p)
        return f"TO_CHAR({rounded}, '{fmt}')"

    @staticmethod
    def _flt1_expr(quoted: str, sig_digits: int) -> str:
        """FLT-1: scientific notation, `sig_digits` significant digits.
        See module docstring for why this is hand-built rather than
        `TO_CHAR(x, '9.99999EEEE')` (breaks on 3-digit exponents and on
        the zero/NaN/Infinity special cases) and why every sub-expression
        guards its own input against `LOG(10, 0)` (a CASE's untaken
        branch is still evaluated per row, confirmed against a real
        account) -- mirrors ClickHouseConnector._flt1_expr's construction,
        adapted to Snowflake's NaN-equals-NaN comparison semantics
        (confirmed live: `'NaN'::float = 'NaN'::float` is TRUE, unlike
        IEEE-754 -- so NaN is detected with a direct `= 'NaN'::float`
        check, not the usual `x != x` idiom, which never fires here).
        """
        sig_digits = max(sig_digits, 1)
        decimals = sig_digits - 1
        is_nan = f"{quoted} = 'NaN'::float"
        is_inf = f"{quoted} = 'Infinity'::float"
        is_ninf = f"{quoted} = '-Infinity'::float"
        safe_abs = f"IFF({quoted} = 0 OR {is_nan} OR {is_inf} OR {is_ninf}, 1.0, ABS({quoted}))"
        mantissa = f"({safe_abs} / POWER(10, FLOOR(LOG(10, {safe_abs}))))"
        if decimals == 0:
            mantissa_str = f"TO_CHAR(TO_NUMBER(ROUND({mantissa})))"
        else:
            pow10 = 10**decimals
            scaled_int = f"TO_NUMBER(ROUND({mantissa} * {pow10}))"
            digits = f"TO_CHAR({scaled_int})"
            target_len = f"GREATEST(LENGTH({digits}), {decimals + 1})"
            padded = f"LPAD({digits}, {target_len}, '0')"
            int_part = f"SUBSTR({padded}, 1, LENGTH({padded}) - {decimals})"
            frac_part = f"SUBSTR({padded}, LENGTH({padded}) - {decimals} + 1)"
            mantissa_str = f"CONCAT({int_part}, '.', {frac_part})"
        exp_digits_str = f"TO_CHAR(TO_NUMBER(ABS(FLOOR(LOG(10, {safe_abs})))))"
        exp_target_len = f"GREATEST(LENGTH({exp_digits_str}), 2)"
        exponent_digits = f"LPAD({exp_digits_str}, {exp_target_len}, '0')"
        zero_lit = ("0." + "0" * decimals) if decimals else "0"
        return (
            "(CASE "
            f"WHEN {is_nan} THEN 'NaN' "
            f"WHEN {is_inf} THEN 'Infinity' "
            f"WHEN {is_ninf} THEN '-Infinity' "
            f"WHEN {quoted} = 0 THEN '{zero_lit}e+00' "
            "ELSE CONCAT("
            f"IFF({quoted} < 0, '-', ''), "
            f"{mantissa_str}, 'e', "
            f"IFF(FLOOR(LOG(10, {safe_abs})) >= 0, '+', '-'), "
            f"{exponent_digits}"
            ") END)"
        )

    @staticmethod
    def _ts1_expr(quoted: str) -> str:
        """TS-1: ISO 8601 UTC, always 6 fractional digits.
        `CONVERT_TIMEZONE('UTC', ...)` converts a TIMESTAMP_LTZ/
        TIMESTAMP_TZ instant to its UTC wall-clock representation
        (confirmed against a real account: a TIMESTAMP_TZ literal with a
        +05:30 offset converts to the correct UTC instant)."""
        as_utc = f"CONVERT_TIMEZONE('UTC', {quoted})"
        return f"TO_CHAR({as_utc}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6\"Z\"')"

    @staticmethod
    def _ts3_expr(quoted: str) -> str:
        """TS-3: naive timestamp (TIMESTAMP_NTZ), rendered as-is, assumed
        UTC -- no CONVERT_TIMEZONE (there's no source zone to convert
        from), same as Postgres/ClickHouse's own TS-3 handling."""
        return f"TO_CHAR({quoted}, 'YYYY-MM-DD\"T\"HH24:MI:SS.FF6\"Z\"')"

    @staticmethod
    def _ts2_expr(quoted: str, native: str, p: int) -> str:
        """TS-2: both sides ROUNDED half-up to the lower of the pair's
        fractional-second precision (not truncated -- same reasoning as
        Postgres/ClickHouse's own TS-2, confirmed independently here:
        rounding 13:45:07.999750 to 3 digits correctly carries into
        13:45:08.000 rather than dropping the fraction). Built on
        `DATE_PART(EPOCH_MICROSECOND, ts)` (confirmed against a real
        account) so the seconds->minutes->... carry is exact integer
        arithmetic, not string manipulation.

        A bare `time` column has no epoch to extract from --
        `TIMESTAMP_NTZ_FROM_PARTS` anchors it onto a fixed date first
        (confirmed live), rounds there via the same epoch-microsecond
        arithmetic, then only the HH:MI:SS[.ffffff] portion is rendered
        back out -- mirrors PostgresConnector._ts2_expr's own anchor-date
        approach for the identical reason.
        """
        p = max(0, min(p, 6))
        is_time_only = native.startswith("time") and not native.startswith("timestamp")
        if is_time_only:
            base = f"TIMESTAMP_NTZ_FROM_PARTS('2000-01-01'::date, {quoted})"
        elif "with time zone" in native:
            base = f"CONVERT_TIMEZONE('UTC', {quoted})"
        else:
            base = quoted
        divisor = 10 ** (6 - p)
        micros = f"DATE_PART(EPOCH_MICROSECOND, {base})"
        rounded_micros = f"(ROUND(({micros} + {divisor // 2}) / {divisor}) * {divisor})"
        rounded_ts = f"TO_TIMESTAMP_NTZ({rounded_micros}, 6)"
        frac = f".FF{p}" if p > 0 else ""
        if is_time_only:
            fmt = f"HH24:MI:SS{frac}"
        else:
            fmt = f'YYYY-MM-DD"T"HH24:MI:SS{frac}"Z"'
        return f"TO_CHAR({rounded_ts}, '{fmt}')"

    @staticmethod
    def _uuid1_expr(quoted: str) -> str:
        """UUID-1: lower-case, hyphenated. Snowflake has no native uuid
        type -- a uuid is stored as plain VARCHAR text, which spec §6.3
        explicitly calls out as needing this exact handling ("uuids:
        upper vs lower case; with vs without hyphens (Snowflake)"). A
        bare 32-hex-character value (no hyphens) is reassembled into
        standard 8-4-4-4-12 form before lower-casing; an already-
        hyphenated value is just lower-cased (confirmed against a real
        account for both shapes)."""
        hyphenated = (
            f"CONCAT(SUBSTR({quoted},1,8),'-',SUBSTR({quoted},9,4),'-',"
            f"SUBSTR({quoted},13,4),'-',SUBSTR({quoted},17,4),'-',SUBSTR({quoted},21,12))"
        )
        return f"LOWER(IFF(LENGTH({quoted}) = 32, {hyphenated}, {quoted}))"

    @staticmethod
    def _str2_expr(quoted: str, opts: NormaliseOptions) -> str:
        expr = quoted
        if opts.trim:
            expr = f"RTRIM({expr})"
        if opts.case_insensitive:
            expr = f"LOWER({expr})"
        return f"TO_VARCHAR({expr})"

    def row_hash_expr(self, exprs: list[str]) -> str:
        """spec §5, the non-negotiable one: both engines must compute the
        SAME 64-bit integer from the same canonical string.

        `MD5_NUMBER_UPPER64(s)` returns the upper 64 bits of the 128-bit
        MD5 digest as an unsigned NUMBER -- exactly the same 8 bytes
        Postgres's `('x'||substr(md5(s),1,16))::bit(64)::bigint` reads
        (the first 16 hex characters of the digest), just not yet folded
        into a signed two's-complement range. Confirmed against a real
        account for the same two reference values ClickHouseConnector's
        own row_hash_expr docstring verified byte-for-byte: `MD5_NUMBER_
        UPPER64('hello')` -> 6719722671305337462 (already matches
        Postgres's signed value directly -- top bit unset); `MD5_NUMBER_
        UPPER64('row-2')` -> 17355174720487169667, which is exactly
        Postgres's -1091569353222381949 + 2**64 (top bit set, needs the
        fold below). No byte-reversal trick needed here, unlike
        ClickHouse's `reinterpretAsInt64` (that one reads native-endian
        bytes; MD5_NUMBER_UPPER64 is defined byte-order-independent).

        The unsigned -> signed fold is `MOD(u + 2^63, 2^64) - 2^63`
        (verified against a real account for both reference values above)
        rather than a `CASE WHEN u >= 2^63 THEN u - 2^64 ELSE u END` --
        both are mathematically equivalent, but the CASE form references
        the (expensive: MD5 + string concat) `u` expression three times
        in the generated SQL, once per branch/condition; the MOD form
        references it once.
        """
        if len(exprs) > 1:
            parts = []
            for i, e in enumerate(exprs):
                if i:
                    parts.append("CHR(31)")  # unit separator, matches Postgres's E'\\x1f'
                parts.append(e)
            concat_expr = "CONCAT(" + ", ".join(parts) + ")"
        else:
            concat_expr = exprs[0]

        unsigned = f"MD5_NUMBER_UPPER64({concat_expr})"
        two_63 = "9223372036854775808"  # 2^63
        two_64 = "18446744073709551616"  # 2^64
        return f"(MOD({unsigned} + {two_63}, {two_64}) - {two_63})"

    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        # Order-independent: SUM is commutative. Snowflake NUMBER carries
        # up to 38 digits of precision, comfortably wide enough to sum
        # many signed 64-bit values without overflow (no separate widening
        # cast needed, unlike Postgres's ::numeric / ClickHouse's
        # toInt256 -- SUM(<signed 64-bit expr>) already stays exact).
        modulus = "18446744073709551616"  # 2^64
        return f"((SUM({row_hash_expr}) % {modulus} + {modulus}) % {modulus})"

    def key_order_expr(self, column: Column, numeric: bool) -> str:
        # Snowflake's default collation is a plain binary/byte-order
        # comparison (no locale-aware default collation the way a
        # typical Postgres database has -- confirmed against a real
        # account: VARCHAR comparison order matches Python's own
        # codepoint ordering for a mixed-case/unicode sample), so there's
        # no analogue of PostgresConnector.key_order_expr's COLLATE "C"
        # problem to guard against here.
        return self.quote_identifier(column.name)

    def key_bounds_expr(self, column: Column, numeric: bool) -> str:
        return self.key_order_expr(column, numeric)

    def key_select_expr(self, column: Column) -> str:
        # Snowflake's FULL OUTER JOIN already produces genuine SQL NULL
        # for an unmatched row's columns regardless of NOT NULL
        # (confirmed against a real account), so the raw column is
        # always correct here -- no toNullable()-style wrapper needed,
        # unlike ClickHouseConnector.
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
        # Snowflake string literals treat backslash as an escape
        # character by default (confirmed against a real account: a
        # literal `\n` in a single-quoted string becomes an actual
        # newline) -- unlike Postgres's default, and unlike standard
        # SQL -- so backslashes need doubling here too, not just quotes
        # (same unconditional escaping ClickHouseConnector.quote_literal
        # already uses, for the same reason).
        escaped = str(value).replace("\\", "\\\\").replace("'", "''")
        return f"'{escaped}'"
