from contextlib import contextmanager
from datetime import datetime, timezone
import gzip
import json

import pytest

from advisor.research.artifacts import ArtifactStore
from advisor.research.capsules import build_capsule
from advisor.research.contracts import ResearchBoundary, ResearchSubject, canonical_json
from advisor.research.query import CapsuleQuery, QueryDenied
from advisor.research.agents.runner import _validate_query_plan, _execute_host_query_plan


@contextmanager
def query_capsule(tmp_path, streaming, rows, *, unlimited=False):
    store = ArtifactStore(tmp_path / 'artifacts')
    payload = {'rows': rows}
    if streaming:
        source = tmp_path / 'source.gz'
        with gzip.open(source, 'wb') as f:
            for row in rows:
                f.write(canonical_json({'kind': 'bar', 'row': row}) + b'\n')
        backing = store.adopt_file(source)
        payload = {'__a_hunter_query_artifact__': backing.content_hash,
                   '__a_hunter_query_format__': 'ndjson-v1', 'summary': {}}
    capsule = build_capsule(
        label='query-regression', instructions='inspect declared data',
        subject=ResearchSubject(code='000001'),
        boundary=ResearchBoundary(as_of=datetime(2026, 9, 7, tzinfo=timezone.utc)),
        inputs={'history@1': {'product': 'history@1', 'payload': payload}}, output_schema={},
        query_budget=None if unlimited else 10, max_result_rows=None if unlimited else 100,
        max_result_bytes=None if unlimited else 20000,
        artifact_store=store,
    )
    try:
        yield capsule
    finally:
        capsule.cleanup()


@pytest.mark.parametrize('streaming', [False, True])
def test_missing_amount_never_becomes_zero_or_partial_total(tmp_path, streaming):
    rows = [{'code': '000001', 'trade_date': '2026-09-04', 'amount': None},
            {'code': '000002', 'trade_date': '2026-09-04', 'amount': 20}]
    with query_capsule(tmp_path, streaming, rows) as capsule:
        query = CapsuleQuery(capsule.root)
        for params in ({'codes': ['000001']}, {}):
            result = query.execute('history@1', 'aggregate', metric='sum', value_field='amount', **params)
            assert result['value'] is None
            assert result['missing_count'] == 1
        window = query.execute('history@1', 'window_compare', field='amount', metric='sum',
                               current_start='2026-09-04', current_end='2026-09-04',
                               previous_start='2026-09-03', previous_end='2026-09-03')
        assert window['current']['value'] is None
        assert window['previous']['value'] is None
        assert window['delta'] is None


@pytest.mark.parametrize('streaming', [False, True])
def test_breadth_uses_returns_and_does_not_filter_away_explicit_field(tmp_path, streaming):
    rows = [{'close': 10, 'change_pct': -2}, {'close': 20, 'change_pct': 1}]
    with query_capsule(tmp_path, streaming, rows) as capsule:
        query = CapsuleQuery(capsule.root)
        with pytest.raises(QueryDenied, match='change_pct'):
            query.execute('history@1', 'breadth', field='close')
        result = query.execute('history@1', 'breadth', field='change_pct')
        assert result['count'] == 2
        assert result['advance_ratio'] == .5


def test_planner_rejects_price_as_breadth_before_executing_queries():
    with pytest.raises(ValueError, match='change_pct'):
        _validate_query_plan({'queries': [{'query_id': 'bad-breadth', 'product': 'history@1',
                            'operation': 'breadth', 'params_json': '{"field":"close"}'}]},
                            queryable_products=('history@1',), query_budget=10, max_result_rows=100)


@pytest.mark.parametrize('streaming', [False, True])
def test_window_sums_do_not_compare_different_observed_session_counts(tmp_path, streaming):
    rows = [{'code': '000001', 'trade_date': date, 'amount': 10}
            for date in ['2026-08-31', '2026-09-01', '2026-09-04']]
    with query_capsule(tmp_path, streaming, rows) as capsule:
        result = CapsuleQuery(capsule.root).execute('history@1', 'window_compare',
            field='amount', metric='sum', current_start='2026-09-03', current_end='2026-09-04',
            previous_start='2026-08-31', previous_end='2026-09-01')
        assert result['current']['observed_session_count'] == 1
        assert result['previous']['observed_session_count'] == 2
        assert result['delta'] is None
        assert result['comparison_issue'] == 'unequal_session_coverage'


def test_host_plan_scans_original_stream_once_and_keeps_results_and_audit(tmp_path, monkeypatch):
    import advisor.research.query as module
    rows = [{'code': '000001', 'trade_date': date, 'amount': 10}
            for date in ['2026-07-01', '2026-09-03', '2026-09-04']]
    with query_capsule(tmp_path, True, rows) as capsule:
        source = capsule.root / 'query-data' / 'history__1.json'
        opened = []
        original = module.gzip.open
        def track(path, *args, **kwargs):
            if path == source:
                opened.append(path)
            return original(path, *args, **kwargs)
        monkeypatch.setattr(module.gzip, 'open', track)
        requests = [{'query_id': f'q{i}', 'product': 'history@1', 'operation': 'aggregate',
                     'params_json': json.dumps({'metric': metric, 'value_field': 'amount',
                         'start_date': '2026-09-03', 'end_date': '2026-09-04'})}
                    for i, metric in enumerate(['sum', 'mean', 'max'])]
        results = _execute_host_query_plan(capsule, {'queries': requests})
        assert [r['result']['value'] for r in results] == [20, 10, 10]
        assert len(opened) == 1
        audit = [json.loads(line) for line in capsule.query_log_path.read_text().splitlines()]
        assert [r['result_hash'] for r in audit] == [r['result_hash'] for r in results]


@pytest.mark.parametrize("streaming", [False, True])
def test_unlimited_queries_keep_full_results_and_audit_across_reopening(tmp_path, streaming):
    rows = [{"code": f"{i:06d}", "trade_date": "2026-09-07", "close": i,
             "description": "研究数据" * 110} for i in range(5201)]
    with query_capsule(tmp_path, streaming, rows, unlimited=True) as capsule:
        query = CapsuleQuery(capsule.root)
        result = query.execute("history@1", "filter", codes=[row["code"] for row in rows])
        assert len(result) == 5201
        assert query.audit.returned_bytes > 2_000_000
        # The old fixed rank maximum was 500, independently of Agent budgets.
        ranked = query.execute("history@1", "rank", field="close", limit=1200)
        assert len(ranked) == 1200
        assert ranked[0]["close"] == 5200
        grouped = query.execute("history@1", "group_by", metric="count", group_by="code")
        assert len(grouped["groups"]) == 5201
        for _ in range(41):
            assert CapsuleQuery(capsule.root).execute("history@1", "aggregate", metric="count")["value"] == 5201
        reopened = CapsuleQuery(capsule.root)
        assert reopened.audit.query_count == 44
        assert reopened.audit.returned_rows > 11_000
        with pytest.raises(QueryDenied, match="not declared"):
            reopened.execute("undeclared@1", "aggregate", metric="count")
        assert len(capsule.query_log_path.read_text().splitlines()) == 44


def test_current_agent_contract_retires_legacy_budget_values_without_changing_files(tmp_path):
    from advisor.research.contracts import AgentManifest
    from advisor.research.agents.runner import _query_plan_schema, _query_planning_prompt
    raw = {"agent": "market@1", "title": "市场", "instructions": "研究已声明数据。",
           "required_products": ["history@1"], "query_budget": 1,
           "max_result_rows": 1, "max_result_bytes": 1024}
    original = json.dumps(raw)
    agent = AgentManifest.model_validate(raw)
    assert json.dumps(raw) == original
    assert agent.query_budget is agent.max_result_rows is agent.max_result_bytes is None
    assert "maxItems" not in _query_plan_schema(("history@1",), agent.query_budget)["properties"]["queries"]
    assert "不设预算上限" in _query_planning_prompt(agent, ("history@1",))
    plan = {"queries": [{"query_id": f"q{i}", "product": "history@1", "operation": "rank",
                         "params_json": json.dumps({"field": "close", "limit": 1001 + i,
                            "codes": [f"{j:06d}" for j in range(5201)]})} for i in range(41)]}
    assert _validate_query_plan(plan, queryable_products=("history@1",),
                               query_budget=None, max_result_rows=None) == plan
