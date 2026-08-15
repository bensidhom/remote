"""Shared spreadsheet-formula neutralization for downloadable CSV values."""

from __future__ import annotations

from typing import TypeVar


_T = TypeVar("_T")
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def spreadsheet_safe_value(value: _T) -> _T | str:
    """Return ``value`` unchanged unless it is formula-like spreadsheet text.

    Spreadsheet applications can evaluate text whose first non-whitespace
    character is ``=``, ``+``, ``-``, or ``@``.  Prefixing the *original*
    string with an apostrophe makes it literal while preserving every byte of
    its whitespace and content.  Non-string values retain their original type
    so genuine numeric CSV cells are not converted to text.
    """

    if not isinstance(value, str):
        return value
    if value.lstrip().startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


__all__ = ["spreadsheet_safe_value"]
