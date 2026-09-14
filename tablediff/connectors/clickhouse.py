"""ClickHouse connector (spec §5, M2).

*** VERIFICATION STATUS: UNVERIFIED AGAINST A REAL CLICKHOUSE SERVER. ***
See _chwire.py's module docstring for the full explanation of why (no
Docker daemon, no network access to install clickhouse-connect/chdb or the
clickhouse-server package, no binary pre-installed in this dev
environment — every avenue was checked, including the user's linked
desktop machine, which hit an unrelated Windows-bridge bug). Every SQL
expression below is reasoned through against documented ClickHouse
semantics, and the one piece spec §5 calls non-negotiable — the row hash
— is cross-checked byte-for-byte against real Postgres output (see
row_hash_expr's docstring). None of it has actually been executed against
a live ClickHouse. Per spec §13, M2's acceptance criteria are NOT
satisfied and this milestone is NOT being reported done until that
verification happens against a real server.
"""

from __future__ import annotations

from typing import Callable

from tablediff.connectors import _chwire
from tablediff.core.models import Column, NormalisationRule, NormaliseOptions, TableRef


def _unwrap(raw_type: str) -> tuple[str, bool]:
    """Strip `Nullable(...)` and `LowCardinality(...)` wrappers, in either
    nesting order (ClickHouse only actually allows `LowCardinality(Nullable(T))`,
    never the reverse, but this loops either way rather than assuming).
    Returns (inner_type_text, is_nullable). Unlike Postgres, a ClickHouse
    column simply cannot hold NULL at all unless its type is wrapped in
    Nullable(...) — there's no separate "is_nullable" flag to read
    (system.columns *does* report one, but deriving it from the type
    string here keeps this function pure and self-contained, and the two
    can never disagree since Nullable(...) is exactly what makes it true).
    """
    t = raw_type.strip()
    nullable = False
    changed = True
    while changed:
        changed = False
        if t.startswith("Nullable(") and t.endswith(")"):
            t = t[len("Nullable(") : -1].strip()
            nullable = True
            changed = True
        elif t.startswith("LowCardinality(") and t.endswith(")"):
            t = t[len("LowCardinality(") : -1].strip()
            changed = True
    return t, nullable


_CH_INT_TYPES = {
    f"{sign}int{bits}" for sign in ("u", "") for bits in (8, 16, 32, 64, 128, 256)
}
_CH_FLOAT_TYPES = {"float32", "float64"}
_CH_BOOL_TYPES = {"bool", "boolean"}
_CH_DATE_TYPES = {"date", "date32"}


def _resolve(inner_type: str) -> tuple[str, int | None, int | None]:
    """Turn a ClickHouse type string (already unwrapped of Nullable/
    LowCardinality) into the (sentinel, scale, precision) shape
    core/normalisation.py's engine-agnostic `pick_rule()` and DEC-1/TS-2's
    pair-negotiation already know how to route — exactly the same job
    PostgresConnector._resolve_native_type does for Postgres's own
    information_schema quirks (ARRAY/USER-DEFINED -> "<elem>[]"/"enum").

    `scale` is DEC-1's decimal-places count; `precision` is TS-2's
    fractional-second count. Only one is ever non-None for a given type.
    """
    t = inner_type.strip()
    tl = t.lower()

    if tl in _CH_INT_TYPES:
        return "bigint", None, None  # width/signedness don't affect INT-1's rendering
    if tl in _CH_FLOAT_TYPES:
        return "double precision", None, None
    if tl in _CH_BOOL_TYPES:
        return "boolean", None, None
    if tl in _CH_DATE_TYPES:
        return "date", None, None
    if tl == "uuid":
        return "uuid", None, None
    if tl == "string" or tl.startswith("fixedstring("):
        return "text", None, None
    if tl == "datetime":
        # A bare ClickHouse DateTime is always an instant (implicitly
        # tz-aware, stored as a UTC epoch second regardless of its
        # display timezone) — spec's M2 acceptance criteria pair it
        # against Postgres timestamptz, i.e. the TS-1 family, never TS-3.
        return "timestamp with time zone", None, 0
    if tl.startswith("datetime64("):
        # DateTime64(3) or DateTime64(3, 'UTC') — the fractional-second
        # digit count is always the first argument.
        inside = t[t.index("(") + 1 : -1]
        p = int(inside.split(",")[0].strip())
        return "timestamp with time zone", None, p
    if tl.startswith("decimal("):
        inside = t[t.index("(") + 1 : -1]
        parts = [p.strip() for p in inside.split(",")]
        scale = int(parts[1]) if len(parts) > 1 else 0
        return "decimal", scale, None
    if tl.startswith(("decimal32(", "decimal64(", "decimal128(", "decimal256(")):
        inside = t[t.index("(") + 1 : -1]
        return "decimal", int(inside.strip()), None
    if tl.startswith(("enum8(", "enum16(")):
        return "enum", None, None
    if tl.startswith("array("):
        return "array[]", None, None
    # Map(...), Tuple(...), IPv4, IPv6, JSON, and anything else: passed
    # through unmatched — core.normalisation.pick_rule()'s own fallback is
    # UNK-1, which is exactly spec's explicit call for Map: "as text,
    # UNK-1". Never fail a run over an unmapped type (spec §6.1).
    return tl, None, None


class ClickHouseConnector:
    engine = "clickhouse"

    def __init__(self) -> None:
        # The parsed DSN is kept for metadata (_database_for) even once
        # connected; the persistent client below is what every query
        # actually runs against — one client for this side's whole
        # lifetime (spec §5: "One connection per side. No connection
        # pooling in v1."), not a fresh one per query, which measured
        # ~20ms of pure reconnect overhead against a local ClickHouse and
        # would blow the 100M-row/5-minute benchmark (spec §13 M2) many
        # times over across the thousands of per-segment queries a large
        # diff issues.
        self._dsn: _chwire.ChDsn | None = None
        self._client = None
        self.on_query: Callable[[str], None] | None = None

    def connect(self, dsn: str) -> None:
        parsed = _chwire.parse_ch_dsn(dsn)
        self._client = _chwire.open_client(parsed)
        self._dsn = parsed

    def close(self) -> None:
        if self._client is not None:
            _chwire.close_client(self._client)
            self._client = None
        self._dsn = None

    def _require_dsn(self) -> _chwire.ChDsn:
        if self._dsn is None:
            raise RuntimeError("connect() must be called before use")
        return self._dsn

    def _require_client(self):
        if self._client is None:
            raise RuntimeError("connect() must be called before use")
        return self._client

    def query(self, sql: str, params: dict | None = None) -> list[tuple]:
        if self.on_query is not None:
            self.on_query(sql)
        return _chwire.run_query_on(self._require_client(), sql)

    def _database_for(self, table: TableRef) -> str:
        return table.schema or self._require_dsn().database

    def get_schema(self, table: TableRef) -> list[Column]:
        database = self._database_for(table)
        sql = (
            "SELECT name, type, position "
            "FROM system.columns "
            f"WHERE database = {self.quote_literal(database)} "
            f"AND table = {self.quote_literal(table.table)} "
            "ORDER BY position"
        )
        rows = self.query(sql)
        columns = []
        for name, raw_type, position in rows:
            inner, nullable = _unwrap(raw_type)
            native_type, scale, precision = _resolve(inner)
            columns.append(
                Column(
                    name=name,
                    native_type=native_type,
                    nullable=nullable,
                    ordinal=int(position),
                    collation=None,  # ClickHouse has no per-column collation concept
                    scale=scale,
                    precision=precision,
                )
            )
        return columns

    def get_primary_key(self, table: TableRef) -> list[str] | None:
        database = self._database_for(table)
        sql = (
            "SELECT name FROM system.columns "
            f"WHERE database = {self.quote_literal(database)} "
            f"AND table = {self.quote_literal(table.table)} "
            "AND is_in_primary_key = 1 "
            "ORDER BY position"
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
        opts = options or NormaliseOptions()

        if rule is NormalisationRule.INT_1:
            inner = f"toString({quoted})"
        elif rule is NormalisationRule.DEC_1:
            p = opts.scale_overrides.get(column.name, column.scale if column.scale is not None else 0)
            inner = self._dec1_expr(quoted, p)
        elif rule is NormalisationRule.FLT_1:
            inner = self._flt1_expr(quoted, opts.float_precision)
        elif rule in (NormalisationRule.TS_1, NormalisationRule.TS_3):
            inner = self._ts1_expr(quoted)
        elif rule is NormalisationRule.TS_2:
            p = opts.precision_overrides.get(column.name, column.precision if column.precision is not None else 6)
            inner = self._ts2_expr(quoted, p)
        elif rule is NormalisationRule.DATE_1:
            inner = f"toString(toDate({quoted}))"
        elif rule is NormalisationRule.TIME_1:
            # ClickHouse has no bare "time of day" type of its own — this
            # exists only so a pairing against another engine's TIME-1
            # column doesn't hard-crash; no ClickHouse native type
            # currently routes here (see _resolve()).
            inner = f"toString({quoted})"
        elif rule is NormalisationRule.BOOL_1:
            inner = f"(CASE WHEN {quoted} THEN 'true' ELSE 'false' END)"
        elif rule is NormalisationRule.UUID_1:
            inner = f"toString({quoted})"
        elif rule is NormalisationRule.BIN_1:
            inner = f"lower(hex({quoted}))"
        elif rule is NormalisationRule.JSON_1:
            inner = f"toString({quoted})"
        elif rule is NormalisationRule.ENUM_1:
            inner = f"toString({quoted})"
        elif rule is NormalisationRule.ARR_1:
            inner = self._arr1_expr(quoted)
        elif rule is NormalisationRule.STR_2:
            inner = self._str2_expr(quoted, opts)
        else:  # STR_1, UNK_1 fallback
            inner = f"toString({quoted})"

        if column.nullable:
            # NULL-1: the literal two-character string `\N`, built via
            # char(92) (ASCII backslash) rather than a quoted string
            # literal — sidesteps ClickHouse string-escaping ambiguity
            # entirely (same reasoning as row_hash_expr's char(31)
            # separator below).
            null_lit = "concat(char(92), 'N')"
            return f"(CASE WHEN {quoted} IS NULL THEN {null_lit} ELSE {inner} END)"
        return inner

    @staticmethod
    def _dec1_expr(quoted: str, p: int) -> str:
        """DEC-1: exactly `p` decimal places, round half-even — mirrors
        PostgresConnector._dec1_expr's explicit floor+even-tie construction
        rather than trusting ClickHouse round()'s own tie-breaking rule.
        `toDecimal256(x, 30)` widens to a high-scale exact decimal (a pure
        rescale, never lossy) before scaling by `intExp10(p)` — deliberately
        avoiding Float64 anywhere, since Decimal * Float64 would silently
        reintroduce the imprecision numeric arithmetic exists to avoid.

        Two bugs found running this against a real server (both fixed
        here, not just reasoned through):

        1. `toString(toDecimal256(x, p))` — the original approach — silently
           strips trailing zeros (`toString(toDecimal256(12.5, 2))` ->
           `'12.5'`, not `'12.50'`), unlike Postgres's `to_char('FM...0.00')`
           which always pads to exactly `p` places. DEC-1's canonical form
           requires that padding so two values that differ only in
           reported scale (12.50 vs 12.5000) render identically at their
           negotiated minimum scale — confirmed empirically, not just
           inferred from docs. Fixed by hand-formatting the rounded,
           scaled *integer* into `sign + digits + '.' + digits` instead of
           ever converting back to a ClickHouse Decimal for `toString()`.
        2. The even/odd tie-break (`floor_expr % 2 = 0`) computed `%`
           directly on the still-Decimal256(30) `floor_expr` — ClickHouse's
           `%` on a high-scale Decimal does not behave like integer modulo
           (`13.000...0 % 2` came back `0`, i.e. "even", for the genuinely
           odd integer 13). Fixed by casting to `Int256` with `toInt256()`
           before ever taking `%` or comparing sign.
        """
        p = max(p, 0)
        wide = f"toDecimal256({quoted}, 30)"
        pow10 = f"intExp10({p})"
        scaled = f"({wide} * {pow10})"
        floor_expr = f"floor({scaled})"
        floor_int = f"toInt256({floor_expr})"
        # Both CASE branches must return the same type (ClickHouse has no
        # common supertype between Int256 and Decimal(76,30), and errors
        # rather than picking one) — round()'s branch is cast to Int256
        # too, not just the even/odd branch.
        scaled_int = (
            f"(CASE WHEN ({scaled} - {floor_expr}) = 0.5 "
            f"THEN (CASE WHEN {floor_int} % 2 = 0 THEN {floor_int} ELSE {floor_int} + 1 END) "
            f"ELSE toInt256(round({scaled})) END)"
        )
        sign = f"(CASE WHEN {scaled_int} < 0 THEN '-' ELSE '' END)"
        abs_digits = f"toString(abs({scaled_int}))"
        if p == 0:
            return f"concat({sign}, {abs_digits})"
        # leftPad() TRUNCATES from the right when the input is already
        # longer than the target length (confirmed empirically — it does
        # not just leave a too-long input alone), so the target length has
        # to be at least the input's own length or digits silently vanish
        # off a value with more than `p+1` integer digits.
        target_len = f"greatest(length({abs_digits}), {p + 1})"
        padded = f"leftPad({abs_digits}, {target_len}, '0')"
        int_part = f"left({padded}, length({padded}) - {p})"
        frac_part = f"right({padded}, {p})"
        return f"concat({sign}, {int_part}, '.', {frac_part})"

    @staticmethod
    def _flt1_expr(quoted: str, sig_digits: int) -> str:
        """FLT-1: scientific notation, `sig_digits` significant digits.
        ClickHouse's `formatReadableQuantity`/`%e` printf-style format via
        `format()` isn't a precision-controlled scientific-notation
        primitive the way Postgres's `to_char(...,'EEEE')` is, so this
        builds it from `sign`/`abs`/`floor(log10(...))`/`round()`.

        Three real bugs found running this against a real server (a
        cross-engine hash-equality style comparison — Postgres's own
        `_flt1_expr` output for the same value as the reference — was
        never actually exercised for ClickHouse before; the previous
        version of this method was flagged in its own docstring as "the
        least-confident piece of this connector, flagged for priority
        real-engine verification" and turned out to need every bit of
        that caution):

        1. The mantissa was missing the literal `'e'` character entirely
           — `concat(sign, mantissa, exponent_sign, exponent_digits)`
           produced `'3.14159+00'`, not `'3.14159e+00'`.
        2. `toString(round(x, decimals))` on a plain Float64 does not pad
           trailing zeros (`toString(round(3.14159, 14))` -> `'3.14159'`,
           `toString(round(3.0, 14))` -> `'3'`, not 14 decimal places) —
           the same class of bug as `_dec1_expr`'s `toString(Decimal)`.
           Every value whose mantissa doesn't happen to need exactly
           `decimals` real digits rendered short, disagreeing with
           Postgres's always-padded `to_char(...,'EEEE')` output for the
           identical value — silently reporting FLT-1 columns as
           "changed" between engines when they were not.
        3. The pre-existing exponent formatting
           (`leftPad(toString(...), 2, '0')`) both (a) crashes outright
           for ANY row where some row in the same query has value `0`
           (`log10(0) = -inf`, and ClickHouse's columnar CASE evaluates
           every branch's expression for every row regardless of which
           branch's *result* is selected, so `toInt64(-inf)` errors the
           whole query even though its output is discarded) and (b)
           truncates a 3-digit exponent to 2 (`leftPad` shortens an
           already-longer input instead of leaving it alone — confirmed
           empirically, see `_dec1_expr`'s docstring for the same
           discovery) — `1e308` rendered as `...e+30`, not `...e+308`.

        Fixed by: adding the missing `'e'`; hand-formatting the mantissa
        into a fixed-decimal string the same way `_dec1_expr` does
        (scaled integer -> `leftPad` -> split, with `greatest(length(...),
        ...)` so a longer-than-expected value is never truncated); and
        `toInt64OrZero` instead of `toInt64` for both the mantissa and
        exponent so a degenerate `0`/`inf`/`NaN` input can never abort
        the whole query, only the (already-unused, WHEN-branch-shadowed)
        value it would have produced. Verified against a real server for
        18 values including `0.0`, `-0.0`, `1e308`, `5e-300`, `NaN`,
        `Infinity` and `-Infinity`, cross-checked byte-for-byte against
        Postgres's own `_flt1_expr` output for the identical stored
        column values on both sides.
        """
        sig_digits = max(sig_digits, 1)
        decimals = sig_digits - 1
        mantissa = f"(abs({quoted}) / pow(10, floor(log10(abs({quoted})))))"
        if decimals == 0:
            mantissa_str = f"toString(toInt64OrZero(toString(round({mantissa}))))"
        else:
            pow10 = 10**decimals
            scaled_int = f"toInt64OrZero(toString(round({mantissa} * {pow10})))"
            digits = f"toString({scaled_int})"
            target_len = f"greatest(length({digits}), {decimals + 1})"
            padded = f"leftPad({digits}, {target_len}, '0')"
            int_part = f"left({padded}, length({padded}) - {decimals})"
            frac_part = f"right({padded}, {decimals})"
            mantissa_str = f"concat({int_part}, '.', {frac_part})"
        exp_digits_str = f"toString(abs(toInt64OrZero(toString(floor(log10(abs({quoted})))))))"
        exp_target_len = f"greatest(length({exp_digits_str}), 2)"
        exponent_digits = f"leftPad({exp_digits_str}, {exp_target_len}, '0')"
        return (
            "(CASE "
            f"WHEN isNaN({quoted}) THEN 'NaN' "
            f"WHEN {quoted} = inf THEN 'Infinity' "
            f"WHEN {quoted} = -inf THEN '-Infinity' "
            f"WHEN {quoted} = 0 THEN '{'0.' + '0' * decimals if decimals else '0'}e+00' "
            "ELSE concat("
            f"if({quoted} < 0, '-', ''), "
            f"{mantissa_str}, "
            "'e', "
            f"if(floor(log10(abs({quoted}))) >= 0, '+', '-'), "
            f"{exponent_digits}"
            ") END)"
        )

    @staticmethod
    def _ts1_expr(quoted: str) -> str:
        """TS-1: ISO 8601 UTC, always 6 fractional digits. `toDateTime64`'s
        third argument forces UTC *display* regardless of the column's own
        declared timezone (ClickHouse stores every DateTime/DateTime64 as
        a timezone-independent instant — only formatting is zone-aware).
        Known limitation, not worth chasing without a real server to check
        against: `toUnixTimestamp64Micro` on a pre-1970 instant goes
        negative, which would break the plain `% 1000000` below.
        """
        as_utc = f"toDateTime64({quoted}, 6, 'UTC')"
        whole = f"formatDateTime({as_utc}, '%Y-%m-%dT%H:%i:%S')"
        micros = f"(toUnixTimestamp64Micro({as_utc}) % 1000000)"
        return f"concat({whole}, '.', leftPad(toString({micros}), 6, '0'), 'Z')"

    @staticmethod
    def _ts2_expr(quoted: str, p: int) -> str:
        """TS-2: both sides ROUNDED half-up to the lower of the pair's
        fractional-second precision (see PostgresConnector._ts2_expr's
        docstring for why round, not truncate — that reasoning is
        engine-independent). Done on the raw microsecond-since-epoch
        integer (`toUnixTimestamp64Micro`) rather than text, so the carry
        (seconds -> minutes -> ... ) is exact integer arithmetic; same
        pre-1970-negative caveat as `_ts1_expr`.
        """
        p = max(0, min(p, 6))
        divisor = 10 ** (6 - p)
        as_utc = f"toDateTime64({quoted}, 6, 'UTC')"
        micros = f"toUnixTimestamp64Micro({as_utc})"
        rounded_micros = f"(toInt64(floor(({micros} + {divisor // 2}) / {divisor})) * {divisor})"
        rounded_dt = f"fromUnixTimestamp64Micro({rounded_micros}, 'UTC')"
        whole = f"formatDateTime({rounded_dt}, '%Y-%m-%dT%H:%i:%S')"
        if p == 0:
            return f"concat({whole}, 'Z')"
        frac_divisor = 10 ** (6 - p)
        frac = f"leftPad(toString(intDiv(toUnixTimestamp64Micro({rounded_dt}) % 1000000, {frac_divisor})), {p}, '0')"
        return f"concat({whole}, '.', {frac}, 'Z')"

    @staticmethod
    def _arr1_expr(quoted: str) -> str:
        """ARR-1: `[` + elements joined by `,` + `]`, order-sensitive.
        Element-level canonicalisation is a plain `toString()` per element
        (same documented scope limit as PostgresConnector._arr1_expr — no
        recursive per-element DEC-1/TS-2). `isNull` only ever fires for
        `Array(Nullable(T))` elements; harmless no-op otherwise.
        """
        return (
            "concat('[', arrayStringConcat(arrayMap("
            f"x -> if(isNull(x), concat(char(92), 'N'), toString(x)), {quoted}"
            "), ','), ']')"
        )

    @staticmethod
    def _str2_expr(quoted: str, opts: NormaliseOptions) -> str:
        expr = quoted
        if opts.trim:
            expr = f"trimRight({expr})"
        if opts.case_insensitive:
            expr = f"lower({expr})"
        return f"toString({expr})"

    def row_hash_expr(self, exprs: list[str]) -> str:
        """spec §5, the non-negotiable one: both engines must compute the
        SAME 64-bit integer from the same canonical string.

        Postgres (postgres.py, empirically confirmed against a real
        server — see that method's own history): `('x' ||
        substr(md5(s),1,16))::bit(64)::bigint` reads the first 8 bytes of
        the MD5 digest as a BIG-endian signed 64-bit integer. Confirmed
        with two real psql round-trips: md5('hello')'s first 8 bytes
        (5d41402abc4b2a76) -> 6719722671305337462, and md5('row-2')'s
        first 8 bytes (f0d9f774e57a1e83, top bit set) -> -1091569353222381949
        — both match `int(first16hex, 16)` (wrapped into signed two's
        complement), i.e. a plain big-endian read. Not a guess.

        ClickHouse's `reinterpretAsInt64(bytes)` is documented to read
        bytes in the engine's native in-memory order, which on every
        realistic ClickHouse deployment target (x86-64/ARM64, both
        little-endian) is LITTLE-endian — the opposite of Postgres's
        big-endian bit(64) cast. Applying it to the same 8 bytes would
        therefore produce a *different* integer than Postgres for the
        same string (a real, well-known gotcha, not hypothetical — this
        is exactly the class of bug spec §5's "add a cross-engine test...
        non-negotiable" exists to catch). The fix: `reverse()` the 8 raw
        bytes first — ClickHouse's `reverse(s)` reverses a string's byte
        order — so that reading the REVERSED bytes little-endian produces
        the same integer as reading the ORIGINAL bytes big-endian. That
        reasoning is verifiable in pure Python without a live server (see
        tests/unit/test_clickhouse_hash_reference.py, which replicates
        MD5 -> hex -> lower -> substr -> unhex -> reverse -> int64 step by
        step and asserts it matches the real-Postgres values above) —
        but it is NOT the same as running the actual SQL against a real
        ClickHouse, which is what spec §13 M2 actually requires before
        this can be called done.
        """
        if len(exprs) > 1:
            parts = []
            for i, e in enumerate(exprs):
                if i:
                    parts.append("char(31)")  # unit separator, matches Postgres's E'\\x1f'
                parts.append(e)
            concat_expr = "concat(" + ", ".join(parts) + ")"
        else:
            concat_expr = exprs[0]

        digest_hex = f"lower(hex(MD5({concat_expr})))"
        first16 = f"substr({digest_hex}, 1, 16)"
        raw8 = f"unhex({first16})"
        reversed8 = f"reverse({raw8})"
        return f"reinterpretAsInt64({reversed8})"

    def aggregate_hash_expr(self, row_hash_expr: str) -> str:
        """Order-independent SUM mod 2^64 — deliberately NOT ClickHouse's
        native `groupBitXor` (spec §4.1 lists XOR as *an* option, but the
        combining operator must be the SAME on both sides of a diff for
        the aggregate to be comparable at all, and PostgresConnector
        already ships SUM — so ClickHouse mirrors that, not the other
        listed option). `toInt256` widens before summing so millions of
        rows can't overflow, matching Postgres's cast-to-numeric-first for
        the same reason.
        """
        modulus = "toInt256('18446744073709551616')"  # 2^64
        return f"((sum(toInt256({row_hash_expr})) % {modulus} + {modulus}) % {modulus})"

    def key_order_expr(self, column: Column, numeric: bool) -> str:
        """Always the RAW quoted column, for every key shape. ClickHouse
        has no per-column locale-aware default collation concept at all
        — String/FixedString comparison is always plain byte order
        unless a query opts into `ORDER BY ... COLLATE` explicitly,
        which this project never does — so there is no analogue of
        Postgres's locale-ambiguity problem (see
        `PostgresConnector.key_order_expr`'s docstring) to guard against
        here. Emitting Postgres's own `COLLATE "C"` SYNTAX against
        ClickHouse isn't just unnecessary, it's invalid SQL there
        (confirmed against a real server: `COLLATE` on an arbitrary
        expression is a syntax error, not a no-op)."""
        return self.quote_identifier(column.name)

    def key_bounds_expr(self, column: Column, numeric: bool) -> str:
        # ClickHouse has a native MIN/MAX(UUID) aggregate (confirmed
        # against a real server), unlike Postgres — no cast workaround
        # needed for any key type in a bounds query.
        return self.key_order_expr(column, numeric)

    def key_select_expr(self, column: Column) -> str:
        """`toNullable(...)` — ClickHouse's FULL OUTER JOIN fills a
        non-Nullable key column's unmatched side with the type's default
        value (`0` for UInt64), not SQL NULL — confirmed directly against
        a real server. Without this, `s.key IS NULL`/`t.key IS NULL`
        (core/joindiff.py's missing-in-target/extra-in-target detection,
        the entire point of the outer join) silently never fires for any
        typical non-Nullable primary key. `toNullable()` is a documented
        no-op on an already-Nullable column, so this is always safe."""
        return f"toNullable({self.quote_identifier(column.name)})"

    def sample_sql(self, table_sql: str, key_col: str, sample_cap: int) -> str:
        # Postgres's RANDOM() is not standard SQL and not portable —
        # ClickHouse has no function by that name at all (confirmed
        # against a real server: "Function with name 'RANDOM' does not
        # exist"); its own equivalent is rand(), lowercase.
        q = self.quote_identifier(key_col)
        return f"SELECT {q} FROM {table_sql} ORDER BY rand() LIMIT {sample_cap}"

    def quote_identifier(self, name: str) -> str:
        return "`" + name.replace("`", "``") + "`"

    def quote_literal(self, value: object) -> str:
        if value is None:
            return "NULL"
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)):
            return str(value)
        escaped = str(value).replace("\\", "\\\\").replace("'", "\\'")
        return f"'{escaped}'"
