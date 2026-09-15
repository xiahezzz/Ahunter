from datetime import datetime, timezone
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient
import pytest
import yaml

from advisor.research.contracts import ResearchSubject
from advisor.research.execution_settings import ExecutionSettingsService, request_execution_policy
from advisor.research.repository import ResearchRepository
from advisor.web.api import create_app
from tests.advisor.test_research_web import _research_workspace


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    (tmp_path / "codex").mkdir()
    (tmp_path / "codex/models_cache.json").write_text(json.dumps({"models": [
        {"slug": name, "visibility": "list", "default_reasoning_level": "medium",
         "supported_reasoning_levels": [{"effort": "medium"}, {"effort": "high"}]}
        for name in ["model-one", "model-two", "model-three"]]}))
    root = _research_workspace(tmp_path)
    policy = {'policy': 'codex@1', 'model': 'model-one', 'reasoning_effort': 'medium',
              'timeout_seconds': 900, 'max_agent_concurrency': 3, 'max_stage_concurrency': 3,
              'max_retries': 1, 'codex_path': '/private/fake-codex'}
    (root / 'config/research/execution/codex.yaml').write_text(yaml.safe_dump(policy))
    return root


def client_for(root):
    return TestClient(create_app(research_root=root,
        research_config_path=root / 'config/advisor.yaml', db_path=root / 'data/test.sqlite'))


def test_settings_save_creates_immutable_policy_without_starting_research(workspace):
    client = client_for(workspace)
    old = (workspace / 'config/research/execution/codex.yaml').read_bytes()
    current = client.get('/api/research/execution-settings')
    assert current.status_code == 200
    assert 'codex_path' not in current.text and '/private' not in current.text
    saved = client.put('/api/research/execution-settings', json={'expected_policy_ref': 'codex@1', 'model': 'model-two', 'reasoning_effort': 'high'})
    assert saved.status_code == 200
    assert saved.json()['policy_ref'] == 'codex@2'
    assert saved.json()['model'] == 'model-two'
    assert saved.json()['reasoning_effort'] == 'high'
    assert (workspace / 'config/research/execution/codex.yaml').read_bytes() == old
    assert not (workspace / 'data/test.sqlite').exists()
    config = yaml.safe_load((workspace / 'config/advisor.yaml').read_text())
    assert config['research']['default_teams'] == []
    assert config['research']['execution_policy'] == 'codex@2'
    assert yaml.safe_load((workspace / 'config/research/execution/codex-v2.yaml').read_text())['codex_path'] == '/private/fake-codex'
    duplicate = client.put('/api/research/execution-settings', json={'expected_policy_ref': 'codex@2', 'model': 'model-two', 'reasoning_effort': 'high'})
    assert duplicate.status_code == 200
    assert len(list((workspace / 'config/research/execution').glob('*.yaml'))) == 2
    assert client.put('/api/research/execution-settings', json={'expected_policy_ref': 'codex@1', 'model': 'model-three', 'reasoning_effort': 'high'}).status_code == 409


@pytest.mark.parametrize('model', ['', 'bad model', '--model', 'model\nfoo', '$(command)', 'a' * 161])
def test_invalid_model_does_not_change_configuration(workspace, model):
    client = client_for(workspace)
    response = client.put('/api/research/execution-settings', json={'expected_policy_ref': 'codex@1', 'model': model, 'reasoning_effort': 'medium'})
    assert response.status_code == 400
    assert client.get('/api/research/execution-settings').json()['model'] == 'model-one'


def test_model_write_rejects_cross_origin_and_extra_process_parameters(workspace):
    client = client_for(workspace)
    payload = {'expected_policy_ref': 'codex@1', 'model': 'model-two', 'reasoning_effort': 'high'}
    assert client.put('/api/research/execution-settings', json=payload, headers={'Origin': 'https://elsewhere.example'}).status_code == 400
    assert client.put('/api/research/execution-settings', json={**payload, 'codex_path': '/tmp/other'}).status_code == 400


def test_new_request_reads_new_model_but_recovery_keeps_pinned_model(workspace):
    repository = ResearchRepository.open(workspace / 'data/test.sqlite')
    config_path = workspace / 'config/advisor.yaml'
    service = ExecutionSettingsService(root=workspace, config_path=config_path)
    runtime = SimpleNamespace(root=workspace, config_path=config_path, repository=repository)
    try:
        now = datetime.now(timezone.utc)
        def submit(identity):
            with repository.transaction():
                return repository.submit_request(team_ref='core@1', subject=ResearchSubject(code='600519'),
                    origin='web', submission_identity=identity, requested_at=now, accepted_at=now)
        first = submit('first')
        assert request_execution_policy(runtime, first.request_id).model == 'model-one'
        service.update(expected_policy_ref='codex@1', model='model-two', reasoning_effort='high')
        second = submit('second')
        assert request_execution_policy(runtime, second.request_id).model == 'model-two'
        assert request_execution_policy(runtime, second.request_id).reasoning_effort == 'high'
        # A fresh runtime sees the original policy, even with a changed default.
        fresh = SimpleNamespace(root=workspace, config_path=config_path, repository=repository)
        assert request_execution_policy(fresh, first.request_id).model == 'model-one'
        assert request_execution_policy(fresh, first.request_id).reasoning_effort == 'medium'
    finally:
        repository.close()


def test_effort_only_update_and_unsupported_combinations(workspace):
    client = client_for(workspace)
    payload = {'expected_policy_ref': 'codex@1', 'model': 'model-one', 'reasoning_effort': 'high'}
    for changes in [{'reasoning_effort': 'ultra'}, {'model': 'unknown'}, {'reasoning_effort': 'high"'}, {'reasoning_effort': None}]:
        assert client.put('/api/research/execution-settings', json={**payload, **changes}).status_code == 400
    assert client.get('/api/research/execution-settings').json()['policy_ref'] == 'codex@1'
    saved = client.put('/api/research/execution-settings', json=payload)
    assert saved.status_code == 200
    assert saved.json()['reasoning_effort'] == 'high'
    assert saved.json()['policy_ref'] == 'codex@2'


@pytest.mark.parametrize('contents', [None, '{broken', '[]', '{"models": null}', '{"models": [{"visibility": "list", "slug": "bad", "supported_reasoning_levels": null}]}'])
def test_missing_or_invalid_cache_preserves_current_pair(workspace, monkeypatch, tmp_path, contents):
    cache_home = tmp_path / 'unavailable'
    cache_home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(cache_home))
    if contents is not None:
        (cache_home / 'models_cache.json').write_text(contents)
    settings = client_for(workspace).get('/api/research/execution-settings').json()
    assert settings['model_options'] == [{'model': 'model-one', 'reasoning_efforts': ['medium'], 'default_reasoning_effort': 'medium'}]


def test_cache_options_filter_hidden_models_and_use_model_specific_efforts(workspace, tmp_path):
    cache = tmp_path / 'codex/models_cache.json'
    cache.write_text(json.dumps({'models': [
        {'slug': 'model-two', 'visibility': 'list', 'default_reasoning_level': 'high',
         'supported_reasoning_levels': [{'effort': 'high'}, {'effort': 'high'}, {'effort': 'bad"'}]},
        {'slug': 'hidden', 'visibility': 'hide', 'supported_reasoning_levels': [{'effort': 'medium'}]},
    ]}))
    client = client_for(workspace)
    settings = client.get('/api/research/execution-settings').json()
    assert settings['known_models'] == ['model-two', 'model-one']
    assert settings['model_options'][0]['reasoning_efforts'] == ['high']
    assert client.put('/api/research/execution-settings', json={
        'expected_policy_ref': 'codex@1', 'model': 'model-two', 'reasoning_effort': 'medium'}).status_code == 400
