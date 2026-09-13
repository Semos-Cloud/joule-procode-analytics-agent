"""Read-only checks on model-generated SQL before it reaches DuckDB.

The model is asked to emit SELECT. This module assumes it will sometimes emit
something else, and refuses anything that is not a single read.
"""

from __future__ import annotations

import re

_FENCE_RE = re.compile(r"```(?:sql)?\s*(.*?)```", re.I | re.S)
_START_RE = re.compile(r"(?is)\b(WITH|SELECT|FROM)\b")
_LIMIT_RE = re.compile(r"\bLIMIT\s+\d+", re.I)
_COMMENT_LINE_RE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK_RE = re.compile(r"/\*.*?\*/", re.S)

# Single-quoted string literals, '' being an escaped quote. Blanked out before
# the checks below, which scan for *keywords* — an award reason really called
# "Above and Beyond the Call of Duty" is data, not a CALL statement, and a value
# holding a semicolon is not a second statement.
_STRING_LITERAL_RE = re.compile(r"'(?:[^']|'')*'")

# Anything that changes state, reaches the filesystem, or opens another catalog.
#
# This list is the second of two layers, and the table functions on the last two
# lines are the reason it exists: they are the only entries reachable from inside
# an otherwise ordinary SELECT. The first layer is `extract_sql`, which cuts
# everything before the first WITH/SELECT/FROM — so a write wrapped around a read
# (`INSERT INTO t SELECT ...`, `CREATE TABLE t AS SELECT ...`) is defused by
# truncation rather than rejected, and arrives here as the bare read. The
# statement keywords below catch the rest, the ones with no read to hide behind.
#
# REPLACE is deliberately absent: `CREATE OR REPLACE` is already handled by that
# truncation, and listing it cost the read-only `replace()` scalar function and
# DuckDB's `SELECT * REPLACE (...)`.
_FORBIDDEN_RE = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|"
    r"COPY|ATTACH|DETACH|INSTALL|LOAD|EXPORT|IMPORT|PRAGMA|"
    r"CALL|SET|RESET|VACUUM|CHECKPOINT|PREPARE|EXECUTE|DEALLOCATE|"
    r"BEGIN|COMMIT|ROLLBACK|GRANT|REVOKE|"
    r"read_csv|read_csv_auto|read_json|read_parquet|read_text|read_blob|"
    r"glob|getenv|sqlite_scan|postgres_scan|delta_scan|iceberg_scan"
    r")\b",
    re.I,
)

_ID_COL_RE = re.compile(r"(^id$|_id$)", re.I)
# "Select the best senders from my team" must not skip the text-to-SQL hop.
_ENGLISH_FROM_RE = re.compile(
    r"\bFROM\s+(my|the|this|that|our|your|their|a|an|his|her)\b",
    re.I,
)

DEFAULT_LIMIT = 50


def extract_sql(text: str) -> str:
    """Take a model reply and return the SQL statement inside it."""
    text = (text or "").strip()
    fenced = _FENCE_RE.search(text)
    if fenced:
        text = fenced.group(1).strip()
    start = _START_RE.search(text)
    if start:
        text = text[start.start() :]
    return text.strip().rstrip(";").strip()


def _scannable(sql: str) -> str:
    """The statement with comments and string *contents* removed.

    What is left has the same keywords and punctuation as the original but no
    data in it, so a keyword check cannot trip over a value the user's own
    reports happen to contain.
    """
    body = _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", sql))
    return _STRING_LITERAL_RE.sub("''", body)


def ensure_readonly(sql: str) -> str:
    """Return a single read statement, or raise ValueError."""
    cleaned = extract_sql(sql)
    if not cleaned:
        raise ValueError("no SQL in the model reply")
    body = _scannable(cleaned)
    if ";" in body:
        raise ValueError("only one SQL statement is allowed")
    if _FORBIDDEN_RE.search(body):
        raise ValueError("SQL is not a read-only SELECT")
    if not _START_RE.match(body.lstrip()):
        raise ValueError("SQL must start with SELECT, WITH or FROM")
    return cleaned


def apply_limit(sql: str, limit: int = DEFAULT_LIMIT) -> str:
    """Cap the result size when the model omitted LIMIT."""
    if _LIMIT_RE.search(sql):
        return sql
    return f"{sql.rstrip()}\nLIMIT {limit}"


def strip_internal_ids(rows: list[dict]) -> list[dict]:
    """Drop identifier columns so they never reach the agent prompt."""
    return [{k: v for k, v in row.items() if not _ID_COL_RE.search(str(k))} for row in rows]


def looks_like_sql(text: str) -> bool:
    """True when *text* is already SQL rather than an English question.

    Requires a FROM so "select the best senders" is not treated as a query, and
    rejects English "from my/the/…" so those questions still go through the
    text-to-SQL hop.
    """
    body = _scannable(text or "")
    if not _START_RE.match(body.lstrip()):
        return False
    if not re.search(r"\bFROM\b", body, re.I):
        return False
    return not _ENGLISH_FROM_RE.search(body)
