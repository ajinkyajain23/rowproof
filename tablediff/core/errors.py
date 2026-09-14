"""Errors that map to CLI exit code 2 ("could not compare") per spec §7.

Kept in core/ (not cli/) because the algorithm is what detects these
conditions; the CLI layer just catches them and picks the exit code.
"""

from __future__ import annotations


class TableDiffError(Exception):
    """Base class for all tablediff-raised errors."""


class NoPrimaryKeyError(TableDiffError):
    def __init__(self, table_name: str):
        super().__init__(
            f"Table '{table_name}' has no primary key and no --key was given. "
            "Pass --key col1,col2 to nominate a unique column set."
        )
        self.table_name = table_name


class NonUniqueKeyError(TableDiffError):
    def __init__(self, table_name: str, key_columns: list[str], duplicate_example: tuple):
        example = ", ".join(f"{c}={v!r}" for c, v in zip(key_columns, duplicate_example))
        super().__init__(
            f"--key {','.join(key_columns)} is not unique on '{table_name}': "
            f"found a duplicate at {example}."
        )
        self.table_name = table_name
        self.key_columns = key_columns
        self.duplicate_example = duplicate_example


class IncompatibleSchemaError(TableDiffError):
    def __init__(self, message: str):
        super().__init__(message)
