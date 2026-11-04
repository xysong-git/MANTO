"""Structural read-only SQL guard for IoTDB statements.

The guard is deliberately conservative and uses three independent checks:

1. string/comment masking (fail closed on unterminated quoting);
2. SQLGlot tokenization to prove that there is exactly one statement, that it
   begins with ``SELECT``, and that no write-capable keyword token appears;
3. a SQLGlot AST statement-type check whenever SQLGlot can parse the statement.

SQLGlot has no IoTDB dialect.  It fails to parse two verified 2.0.6 forms:
wildcard measurement paths (``root.db.g.*``) and interval ``GROUP BY``
(``GROUP BY ([start, end), step)``).  In the Phase-0 workload this affected
10,000 of 24,000 valid statements (6,000 wildcard, 4,000 interval).  For those
statements only, the guard normalizes the verified construct in-memory and
re-parses the result; the normalized statement must parse as a SELECT-family
query.  Every other ``ParseError`` fails closed.  The original SQL is still
executed unchanged and is never automatically rewritten.

This is not a general IoTDB parser and does not claim to accept every valid
IoTDB statement.  It rejects a statement only when a structural check fails.
"""

from __future__ import annotations

import hashlib
import re

from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.tokens import Tokenizer, TokenType

from .errors import UnsafeSQLError

# Keywords that can write, change permissions, change server state, or start a
# non-SELECT statement.  Tokenization already removes quoted identifiers and
# string literals via ``_mask_literals_and_comments``; a token whose text
# matches one of these is therefore an unquoted keyword or identifier.
_FORBIDDEN_KEYWORDS = frozenset(
    {
        "INSERT",
        "UPDATE",
        "DELETE",
        "DROP",
        "ALTER",
        "CREATE",
        "TRUNCATE",
        "GRANT",
        "REVOKE",
        "SET",
        "KILL",
        "LOAD",
        "UNLOAD",
        "FLUSH",
        "CLEAR",
        "START",
        "STOP",
        "MERGE",
        "UPSERT",
        "USE",
        "INTO",
        "OUTFILE",
        "DUMPFILE",
        "CALL",
        "EXEC",
        "EXECUTE",
        "DESCRIBE",
        "EXPLAIN",
        "ANALYZE",
    }
)


def _mask_literals_and_comments(
    sql: str,
    *,
    mask_literals: bool = True,
) -> str:
    """Replace comments with spaces and optionally mask literals.

    The returned string has the same length as the input so token positions
    remain meaningful.  Unterminated literals/comments fail closed.  With
    ``mask_literals=False``, string and quoted-identifier contents are preserved
    so SQLGlot can still parse the statement after comments are removed.
    """

    chars = list(sql)
    index = 0
    length = len(sql)
    while index < length:
        char = sql[index]
        if char == "-" and index + 1 < length and sql[index + 1] == "-":
            end = sql.find("\n", index)
            if end == -1:
                end = length
            for pos in range(index, end):
                chars[pos] = " "
            index = end
        elif char == "/" and index + 1 < length and sql[index + 1] == "*":
            end = sql.find("*/", index + 2)
            if end == -1:
                raise UnsafeSQLError("unterminated block comment")
            end += 2
            for pos in range(index, end):
                chars[pos] = " "
            index = end
        elif char in {"'", '"', "`"}:
            quote = char
            end = index + 1
            while end < length:
                if sql[end] == quote:
                    if end + 1 < length and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    break
                if sql[end] == "\\" and quote == "'":
                    end += 2
                    continue
                end += 1
            else:
                raise UnsafeSQLError("unterminated quoted literal")
            if mask_literals:
                for pos in range(index, end):
                    chars[pos] = " "
            index = end
        else:
            index += 1
    return "".join(chars)


def _strip_comments(sql: str) -> str:
    """Remove comments while preserving string literals and identifiers."""

    return _mask_literals_and_comments(sql, mask_literals=False)


# These are the only two SQLGlot 2.0.6 parse gaps verified by the Phase-0
# workload.  They are normalized only for structural validation; the original
# SQL remains the execution text.
_VERIFIED_WILDCARD_PATH = re.compile(
    r"(?i)\b(?:[A-Za-z_][A-Za-z0-9_]*\.){2,}\*"
)
_VERIFIED_INTERVAL_GROUP_BY = re.compile(
    r"(?is)\bGROUP\s+BY\s*\(\s*\[\s*\d+\s*,\s*\d+\s*\)\s*,\s*\d+\s*ms\s*\)"
)


def _normalize_verified_parse_gap(sql: str) -> str | None:
    """Return a parse-only normalization for a verified SQLGlot gap.

    The substitutions are deliberately limited to the two forms proven by the
    Phase-0 workload.  Literals are masked while locating matches so a string
    containing ``FROM root.db.g.*`` cannot enable the fallback.  The returned
    text is used only to prove the SELECT-family structure; it is never sent to
    IoTDB.
    """

    normalized = _strip_comments(sql)
    changed = False
    for pattern, replacement in (
        (_VERIFIED_WILDCARD_PATH, None),
        (_VERIFIED_INTERVAL_GROUP_BY, "GROUP BY 1"),
    ):
        masked = _mask_literals_and_comments(normalized)
        matches = list(pattern.finditer(masked))
        if not matches:
            continue
        pieces: list[str] = []
        cursor = 0
        for match in matches:
            pieces.append(normalized[cursor : match.start()])
            matched_text = match.group(0)
            if replacement is None:
                # Replace only the trailing ``*`` of the measurement path.
                pieces.append(matched_text[:-1] + "d_0")
            else:
                pieces.append(replacement)
            cursor = match.end()
        pieces.append(normalized[cursor:])
        normalized = "".join(pieces)
        changed = True
    return normalized if changed else None


def _token_statement_check(masked: str) -> None:
    """Token-level single-SELECT and forbidden-keyword check.

    ``masked`` has already had literals, quoted identifiers, and comments
    replaced with spaces, so string contents cannot masquerade as keywords.
    """

    try:
        tokens = Tokenizer().tokenize(masked)
    except Exception as exc:  # noqa: BLE001 - tokenizer failure is fail-closed
        raise UnsafeSQLError(
            f"SQL could not be tokenized safely: {type(exc).__name__}: {exc}"
        ) from exc

    if not tokens:
        raise UnsafeSQLError("SQL contains no executable statement")

    semicolons = [
        index
        for index, token in enumerate(tokens)
        if token.token_type is TokenType.SEMICOLON
    ]
    if len(semicolons) > 1:
        raise UnsafeSQLError("multiple SQL statements are not accepted")
    if semicolons and semicolons[0] != len(tokens) - 1:
        raise UnsafeSQLError("multiple SQL statements are not accepted")

    statement_tokens = (
        tokens[: semicolons[0]] if semicolons else tokens
    )
    if not statement_tokens:
        raise UnsafeSQLError("SQL contains no executable statement")
    if statement_tokens[0].token_type is not TokenType.SELECT:
        raise UnsafeSQLError("only a single read-only SELECT is accepted")

    for token in statement_tokens:
        keyword = token.text.upper()
        if keyword in _FORBIDDEN_KEYWORDS:
            raise UnsafeSQLError(
                "write-capable or unsupported keyword is not accepted: "
                f"{keyword}"
            )


def _is_readonly_query(node: exp.Expression) -> bool:
    """Whether a parsed statement is the SELECT family and nothing else.

    Set operations are read-only when every branch is a SELECT.  DDL, DML,
    ``WITH`` (already rejected by the first-token check), and unknown nodes are
    not accepted.
    """

    if isinstance(node, exp.Select):
        return True
    if isinstance(node, exp.SetOperation):
        return _is_readonly_query(node.this) and _is_readonly_query(node.expression)
    if isinstance(node, exp.Subquery):
        return _is_readonly_query(node.this)
    return False


def _ast_statement_check(sql: str) -> None:
    """Reject a parsed statement outside the SELECT family.

    Comments are removed before parsing.  SQLGlot otherwise treats a trailing
    line comment after a semicolon as a second ``Semicolon`` statement, which
    would reject a valid single SELECT.  Literals are preserved so the AST check
    still sees the real statement.

    A SQLGlot parse failure is tolerated only when the statement matches one of
    the two verified 2.0.6 gaps.  The normalized statement must then parse as a
    SELECT-family query.  Every other parse failure fails closed.
    """

    try:
        statements = parse(_strip_comments(sql))
    except ParseError as exc:
        normalized = _normalize_verified_parse_gap(sql)
        if normalized is None:
            raise UnsafeSQLError(
                "SQL parser failed and the statement does not match a verified "
                "IoTDB 2.0.6 parse gap"
            ) from exc
        try:
            statements = parse(normalized)
        except ParseError as normalized_exc:
            raise UnsafeSQLError(
                "SQL parser failed even after normalizing the verified IoTDB "
                "2.0.6 parse gaps"
            ) from normalized_exc
        except Exception as normalized_exc:  # noqa: BLE001 - fail closed
            raise UnsafeSQLError(
                "SQL structural analysis failed after verified-gap "
                f"normalization: {type(normalized_exc).__name__}: "
                f"{normalized_exc}"
            ) from normalized_exc
    except Exception as exc:  # noqa: BLE001 - unknown parser failures fail closed
        raise UnsafeSQLError(
            f"SQL structural analysis failed: {type(exc).__name__}: {exc}"
        ) from exc

    if len(statements) != 1:
        raise UnsafeSQLError("multiple SQL statements are not accepted")
    statement = statements[0]
    if not _is_readonly_query(statement):
        raise UnsafeSQLError(
            "only a SELECT-family statement is accepted; "
            f"parsed statement type is {type(statement).__name__}"
        )


def validate_readonly_sql(sql: str) -> str:
    from .request_memo import bucket, remember
    cache = bucket('readonly', sql)
    # Exact text plus implementation identity; altered parser/checks cannot
    # inherit a previous successful safety check in the same request.
    key = (sql, parse, Tokenizer, _ast_statement_check, _token_statement_check,
           _FORBIDDEN_KEYWORDS) if cache is not None else None
    if cache is not None and key in cache:
        return cache[key]  # immutable cleaned SQL, not an authorization token
    result = _validate_readonly_sql(sql)
    remember(cache, key, result)  # failures are never memoized
    return result


def _validate_readonly_sql(sql: str) -> str:
    """Return the executable SQL text, or raise :class:`UnsafeSQLError`.

    Accepted: one SELECT-family statement, optional comments/whitespace,
    optional single trailing semicolon, plus the two verified SQLGlot 2.0.6
    parse gaps (wildcard measurement paths and interval ``GROUP BY``).
    Comments are removed from the returned executable text.  Rejected: empty
    input, multiple statements, leading WITH/EXPLAIN/SHOW, write keywords,
    unterminated quoting, any parsed statement type outside the SELECT family,
    and any other unverified ``ParseError``.
    """

    if not isinstance(sql, str) or not sql.strip():
        raise UnsafeSQLError("SQL must be a non-empty string")
    if "\x00" in sql:
        raise UnsafeSQLError("SQL must not contain NUL bytes")

    masked = _mask_literals_and_comments(sql)
    if not masked.strip():
        raise UnsafeSQLError("SQL contains no executable statement")

    _token_statement_check(masked)
    _ast_statement_check(sql)

    cleaned = _strip_comments(sql).strip()
    if cleaned.endswith(";"):
        cleaned = cleaned[:-1].rstrip()
    if not cleaned:
        raise UnsafeSQLError("SQL contains no executable statement")
    return cleaned


def sql_sha256(sql: str) -> str:
    return hashlib.sha256(sql.encode("utf-8")).hexdigest()
