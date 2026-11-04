import pytest
from sqlglot import exp

from tsdb_optimizer.errors import UnsafeSQLError
from tsdb_optimizer import sql_guard
from tsdb_optimizer.sql_guard import (
    _is_readonly_query,
    sql_sha256,
    validate_readonly_sql,
)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s_130 FROM root.test.g_0.d_0",
        "select last s_130 from root.test.g_0.d_0",
        "SELECT s_130 FROM root.test.g_0.d_0 ORDER BY time DESC LIMIT 1",
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time >= 1000",
        "SELECT s_130 FROM root.test.g_0.d_0;",
        "  -- leading comment\nSELECT s_130 FROM root.test.g_0.d_0",
        "/* block comment */ SELECT s_130 FROM root.test.g_0.d_0",
        "SELECT s_130 FROM root.test.g_0.d_0; -- trailing comment",
        "SELECT s_130 FROM root.test.g_0.d_0 /* trailing block */ ;",
        "SELECT s_130 FROM root.test.g_0.d_0; -- trailing\n",
        "SELECT 'DROP TABLE;' AS note FROM root.test.g_0.d_0",
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time IN (SELECT time FROM root.test.g_0.d_0)",
        # SQLGlot cannot parse these two valid IoTDB forms; only the verified
        # gap normalizers may accept them.
        "SELECT * FROM root.dbs.g_0.*",
        "SELECT LAST s_333, s_433 FROM root.dbs.g_0.*;",
        "SELECT s_333 FROM root.dbs.g_0.* WHERE time = 1640971763900;",
        (
            "SELECT s_130 FROM root.test.g_0.d_0 "
            "GROUP BY ([1640966400000, 1640972449000), 10000ms)"
        ),
        # Set operations are read-only when every branch is a SELECT.
        "SELECT s_130 FROM root.test.g_0.d_0 UNION SELECT s_131 FROM root.test.g_0.d_0",
        "SELECT s_130 FROM root.test.g_0.d_0 INTERSECT SELECT s_131 FROM root.test.g_0.d_0",
    ],
)
def test_accepts_readonly_select(sql: str) -> None:
    cleaned = validate_readonly_sql(sql)
    assert cleaned
    assert not cleaned.endswith(";")


@pytest.mark.parametrize(
    "sql",
    [
        "",
        "   -- only a comment",
        "INSERT INTO root.test.g_0.d_0(time, s_130) VALUES(1, 2)",
        "UPDATE root.test.g_0.d_0 SET s_130 = 1",
        "DELETE FROM root.test.g_0.d_0",
        "DROP DATABASE root.test",
        "SHOW DATABASES",
        "EXPLAIN SELECT s_130 FROM root.test.g_0.d_0",
        "WITH x AS (SELECT 1) SELECT * FROM x",
        "SELECT s_130 FROM root.test.g_0.d_0 INTO OUTFILE '/tmp/x'",
        "SELECT s_130 FROM root.test.g_0.d_0; DROP DATABASE root.test",
        "SELECT s_130 FROM root.test.g_0.d_0;;",
        "SELECT 'unterminated FROM root.test.g_0.d_0",
        "SELECT s_130 FROM root.test.g_0.d_0 /* unterminated",
        "SELECT s_130 FROM root.test.g_0.d_0\x00",
    ],
)
def test_rejects_non_readonly_or_ambiguous_sql(sql: str) -> None:
    with pytest.raises(UnsafeSQLError):
        validate_readonly_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE",
        "SELECT s_130 FROM root.test.g_0.d_0 ORDER BY",
        "SELECT s_130 FROM root.test.g_0.d_0 LIMIT",
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE time BETWEEN 1 AND",
        "SELECT s_130 FROM root.test.g_0.d_0 WHERE s_1 =",
        "SELECT s_130 FROM root.test.g_0.d_0 ALIGN BY DEVICE",
    ],
)
def test_unverified_parse_errors_fail_closed(sql: str) -> None:
    with pytest.raises(
        UnsafeSQLError,
        match="does not match a verified IoTDB 2.0.6 parse gap",
    ):
        validate_readonly_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT s_130 FROM root.dbs.g_0.* WHERE",
        "SELECT s_130 FROM root.dbs.g_0.* ORDER BY",
        (
            "SELECT s_130 FROM root.test.g_0.d_0 "
            "GROUP BY ([1640966400000, 1640972449000), 10000ms) WHERE"
        ),
    ],
)
def test_verified_gap_normalization_still_requires_a_safe_tail(sql: str) -> None:
    with pytest.raises(
        UnsafeSQLError,
        match="failed even after normalizing the verified IoTDB 2.0.6 parse gaps",
    ):
        validate_readonly_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT '-- not a comment' AS note FROM root.test.g_0.d_0",
        "SELECT '/* not a comment */' AS note FROM root.test.g_0.d_0",
        'SELECT "s_130" FROM root.test.g_0.d_0',
    ],
)
def test_comment_stripping_does_not_corrupt_literals_or_identifiers(
    sql: str,
) -> None:
    assert validate_readonly_sql(sql)


def test_trailing_comment_is_removed_from_returned_sql() -> None:
    assert validate_readonly_sql(
        "SELECT s_130 FROM root.test.g_0.d_0; -- trailing"
    ) == "SELECT s_130 FROM root.test.g_0.d_0"
    assert validate_readonly_sql(
        "-- leading\nSELECT s_130 FROM root.test.g_0.d_0 /* trailing */"
    ) == "SELECT s_130 FROM root.test.g_0.d_0"


def test_comment_cannot_hide_a_second_statement() -> None:
    with pytest.raises(UnsafeSQLError):
        validate_readonly_sql(
            "SELECT s_130 FROM root.test.g_0.d_0 -- trailing\n; "
            "DROP DATABASE root.test"
        )


def test_hash_is_stable() -> None:
    sql = "SELECT s_130 FROM root.test.g_0.d_0"
    assert sql_sha256(sql) == sql_sha256(sql)
    assert len(sql_sha256(sql)) == 64


def test_ast_statement_type_is_checked_not_just_the_first_token(
    monkeypatch,
) -> None:
    assert _is_readonly_query(exp.Select())
    assert _is_readonly_query(
        exp.Union(this=exp.Select(), expression=exp.Select())
    )
    assert not _is_readonly_query(exp.Insert())
    assert not _is_readonly_query(
        exp.Union(this=exp.Select(), expression=exp.Insert())
    )

    # The token check sees SELECT, but the parsed statement type (here an
    # Insert substituted for the parser result) must still be rejected.
    monkeypatch.setattr(sql_guard, "parse", lambda sql: [exp.Insert()])
    with pytest.raises(UnsafeSQLError, match="SELECT-family"):
        validate_readonly_sql("SELECT s_130 FROM root.test.g_0.d_0")


def test_unexpected_parser_failure_is_fail_closed(monkeypatch) -> None:
    def boom(sql):
        del sql
        raise RuntimeError("parser bug")

    monkeypatch.setattr(sql_guard, "parse", boom)
    with pytest.raises(UnsafeSQLError, match="structural analysis failed"):
        validate_readonly_sql("SELECT s_130 FROM root.test.g_0.d_0")


def test_tokenizer_failure_is_fail_closed(monkeypatch) -> None:
    class BrokenTokenizer:
        def tokenize(self, sql):
            del sql
            raise RuntimeError("tokenizer bug")

    monkeypatch.setattr(sql_guard, "Tokenizer", BrokenTokenizer)
    with pytest.raises(UnsafeSQLError, match="tokenized safely"):
        validate_readonly_sql("SELECT s_130 FROM root.test.g_0.d_0")
