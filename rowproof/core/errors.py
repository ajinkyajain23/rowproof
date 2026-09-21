"""Errors that map to CLI exit code 2 ("could not compare") per spec §7.

Kept in core/ (not cli/) because the algorithm is what detects these
conditions; the CLI layer just catches them and picks the exit code.
"""

from __future__ import annotations


class RowProofError(Exception):
    """Base class for all rowproof-raised errors."""


class NoPrimaryKeyError(RowProofError):
    def __init__(self, table_name: str):
        super().__init__(
            f"Table '{table_name}' has no primary key and no --key was given. "
            "Pass --key col1,col2 to nominate a unique column set."
        )
        self.table_name = table_name


class TableNotFoundError(RowProofError):
    def __init__(self, table_name: str):
        super().__init__(
            f"Table '{table_name}' not found, or it has no visible columns "
            "(check the name, the schema, and that this login can read it)."
        )
        self.table_name = table_name


class KeyColumnNotFoundError(RowProofError):
    def __init__(self, table_name: str, column: str, available: list[str]):
        hint = ""
        by_lower = {c.lower(): c for c in available}
        if column.lower() in by_lower and by_lower[column.lower()] != column:
            hint = (
                f" Did you mean '{by_lower[column.lower()]}'? Column names are "
                "case-sensitive here (Snowflake stores unquoted names in upper case)."
            )
        super().__init__(
            f"Key column '{column}' not found in '{table_name}'.{hint} "
            f"Available columns: {', '.join(available)}."
        )
        self.table_name = table_name
        self.column = column


class NonUniqueKeyError(RowProofError):
    def __init__(self, table_name: str, key_columns: list[str], duplicate_example: tuple):
        example = ", ".join(f"{c}={v!r}" for c, v in zip(key_columns, duplicate_example))
        super().__init__(
            f"--key {','.join(key_columns)} is not unique on '{table_name}': "
            f"found a duplicate at {example}."
        )
        self.table_name = table_name
        self.key_columns = key_columns
        self.duplicate_example = duplicate_example


class IncompatibleSchemaError(RowProofError):
    def __init__(self, message: str):
        super().__init__(message)
