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

# Anything that changes state, reaches the filesystem, or opens another catalog.
_FORBIDDEN_RE = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|REPLACE|TRUNCATE|"
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


def _strip_comments(sql: str) -> str:
    return _COMMENT_LINE_RE.sub(" ", _COMMENT_BLOCK_RE.sub(" ", sql))


def ensure_readonly(sql: str) -> str:
    """Return a single read statement, or raise ValueError."""
    cleaned = extract_sql(sql)
    if not cleaned:
        raise ValueError("no SQL in the model reply")
    body = _strip_comments(cleaned)
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
    body = _strip_comments(text or "")
    if not _START_RE.match(body.lstrip()):
        return False
    if not re.search(r"\bFROM\b", body, re.I):
        return False
    return not _ENGLISH_FROM_RE.search(body)
