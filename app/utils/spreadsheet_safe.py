"""Values written to CSV or Excel exports, made safe to open.

A cell that begins with =, +, -, @ (or a tab or carriage return) is run
as a formula by Excel and LibreOffice. Customer names, email subjects and
notes all reach exports, and any of them can be written by an outsider —
"=HYPERLINK(...)" in a company name becomes a live link, or worse, on the
machine of whoever opens the report. Such text is prefixed with an
apostrophe, which spreadsheets display as plain text.

Numbers stay numbers: a genuine -1500 or +91 phone is not text a formula
can hide in, so numeric-looking strings are left alone.
"""
import re

_TRIGGERS = ('=', '+', '-', '@', '\t', '\r')
_NUMERIC = re.compile(r'^[+-]?[\d\s().,]+$')


def safe_cell(value):
    if not isinstance(value, str) or not value:
        return value
    if value[0] in _TRIGGERS and not _NUMERIC.match(value):
        return "'" + value
    return value


def safe_row(values):
    return [safe_cell(v) for v in values]


def safe_dict(row):
    return {k: safe_cell(v) for k, v in row.items()}
