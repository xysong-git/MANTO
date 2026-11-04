"""Synthetic adversarial cases; memoization is not query-result caching."""
import pytest
from tsdb_optimizer.request_memo import request_scope, bucket
from tsdb_optimizer import sql_guard
from tsdb_optimizer.parsing import SQLParserAdapter
from tsdb_optimizer.errors import UnsafeSQLError, ParserInternalError

SQL='SELECT s1 FROM root.test.d1 WHERE time > 1'


def test_exact_sql_only_and_scoped_cleanup(monkeypatch):
    original=sql_guard._ast_statement_check;calls=[]
    def counted(sql):calls.append(sql);return original(sql)
    monkeypatch.setattr(sql_guard,'_ast_statement_check',counted)
    @request_scope
    def run():
        assert sql_guard.validate_readonly_sql(SQL)==SQL
        assert sql_guard.validate_readonly_sql(SQL)==SQL
        with pytest.raises(UnsafeSQLError):sql_guard.validate_readonly_sql(SQL+'; DELETE FROM root.test.d1')
    run();assert calls.count(SQL)==1
    run();assert calls.count(SQL)==2
    assert bucket('readonly',SQL) is None


def test_changed_validation_implementation_rechecks(monkeypatch):
    @request_scope
    def run():
        sql_guard.validate_readonly_sql(SQL)
        def fail(sql):raise UnsafeSQLError('changed checker')
        monkeypatch.setattr(sql_guard,'_ast_statement_check',fail)
        with pytest.raises(UnsafeSQLError):sql_guard.validate_readonly_sql(SQL)
    run()


def test_private_ast_and_ids_are_not_shared():
    parser=SQLParserAdapter()
    @request_scope
    def run():
        first=parser.parse(SQL,query_id='first')
        first.ast.set('where',None)
        first.warnings.append('tampered')
        second=parser.parse(SQL,query_id='second')
        assert second.query_id=='second' and second.ast.args['where'] is not None
        assert 'tampered' not in second.warnings
        assert parser.extract_features(second).query_id=='second'
        assert parser.parse(SQL+' AND time < 10').query_hash!=second.query_hash
    run()


def test_exception_discards_facts_and_cache_is_bounded():
    @request_scope
    def run():
        for i in range(40):sql_guard.validate_readonly_sql(f'SELECT s{i} FROM root.test.d1')
        assert len(bucket('readonly',SQL))==32
        assert bucket('readonly','x'*65537) is None
        raise RuntimeError('test')
    with pytest.raises(RuntimeError):run()
    assert bucket('readonly',SQL) is None


def test_features_reuse_verified_analysis_and_isolate_mutation(monkeypatch):
    from tsdb_optimizer.parsing import sql_parser
    original = sql_parser._analyze
    calls = []
    def counted(ast):
        calls.append(ast)
        return original(ast)
    monkeypatch.setattr(sql_parser, '_analyze', counted)
    parser = SQLParserAdapter()
    @request_scope
    def run():
        parsed = parser.parse(SQL, query_id='first')
        features = parser.extract_features(parsed)
        features.devices.value.clear()
        features.warnings.append('tampered')
        parsed.ast.set('where', None)
        second = parser.extract_features(parser.parse(SQL, query_id='second'))
        assert second.query_id == 'second'
        assert second.device_count.value == len(second.devices.value) == 1
        assert 'tampered' not in second.warnings
        assert second.filter_count.value == 1
        for field in ('query_hash', 'normalized_template'):
            altered = parsed.model_copy(update={field: 'tampered'})
            with pytest.raises(ParserInternalError):
                parser.extract_features(altered)
    run()
    assert len(calls) == 1
    run()
    assert len(calls) == 2
    assert bucket('features', SQL) is None


def test_feature_cache_invalidates_changed_analyzer(monkeypatch):
    from tsdb_optimizer.parsing import sql_parser
    parser = SQLParserAdapter()
    @request_scope
    def run():
        parsed = parser.parse(SQL)
        parser.extract_features(parsed)
        def broken(ast):
            raise RuntimeError('changed implementation')
        monkeypatch.setattr(sql_parser, '_analyze', broken)
        with pytest.raises(ParserInternalError):
            parser.extract_features(parsed)
    run()


def test_feature_cache_does_not_accept_metadata_or_unsupported_sql():
    parser = SQLParserAdapter()
    @request_scope
    def run():
        parsed = parser.parse(SQL)
        parser.extract_features(parsed)
        with pytest.raises(NotImplementedError):
            parser.extract_features(parsed, metadata={})
        unsupported = parser.parse('SELECT LAST s1 FROM root.test.d1')
        result = parser.extract_features(unsupported)
        assert result.device_count.value is None
        assert len(bucket('features', SQL)) == 1
    run()


def test_analysis_and_feature_memos_are_bounded_and_subclasses_bypass():
    class CustomParser(SQLParserAdapter):
        pass
    @request_scope
    def run():
        custom = CustomParser()
        custom.extract_features(custom.parse(SQL))
        assert bucket('analysis', SQL) == {}
        assert bucket('features', SQL) == {}
        parser = SQLParserAdapter()
        for i in range(40):
            sql = f'SELECT s{i} FROM root.test.d1'
            parser.extract_features(parser.parse(sql))
        assert len(bucket('analysis', SQL)) == 32
        assert len(bucket('features', SQL)) == 32
    run()
