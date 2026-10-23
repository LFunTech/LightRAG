"""Real query routes with external retrieval and ledger boundaries substituted."""
import importlib
import sys
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from lightrag.query_accounting import account_openai_call
from tests.llm.test_query_accounting import Authority


@pytest.fixture
def assembly(monkeypatch):
    monkeypatch.setattr(sys, 'argv', ['lightrag-server'])
    monkeypatch.setenv('AUTH_ACCOUNTS', '')
    monkeypatch.setenv('TOKEN_SECRET', '')
    monkeypatch.setenv('LIGHTRAG_API_KEY', '')
    module = importlib.import_module('lightrag.api.routers.query_routes')
    import lightrag.api.utils_api as auth
    monkeypatch.setattr(auth, 'auth_configured', False)
    monkeypatch.setattr(auth, 'whitelist_patterns', [])
    authority = Authority()
    invoked = []

    class Rag:
        workspace = 'isolated-index'

        async def aquery_llm(self, query, param):
            invoked.append(query)
            async def send():
                return SimpleNamespace(model='embedding-model', usage=SimpleNamespace(prompt_tokens=3, total_tokens=3))
            await account_openai_call('embedding', SimpleNamespace(max_retries=0, base_url='https://model.invalid/v1'),
                                     {'model': 'embedding-model', 'input': [query]}, send)
            return {'llm_response': {'content': 'grounded context', 'llm_generated': False},
                    'data': {'references': [{'reference_id': '1', 'file_path': 'approved.md'}]}}

    async def runtime_check():
        return None

    def build(enabled=True):
        app = FastAPI()
        app.include_router(module.create_query_routes(Rag(), api_key='synthetic-api-key',
            usage_authority=authority if enabled else None, accounting_runtime_check=runtime_check))
        return TestClient(app, raise_server_exceptions=False)
    return build, authority, invoked


def body():
    return {'query': 'synthetic question', 'mode': 'naive', 'only_need_context': True,
            'accounting': {'query_id': 'q-api', 'grant': 'synthetic-grant'}}


def test_authenticated_retrieval_returns_durable_usage_receipt(assembly):
    build, authority, invoked = assembly
    with build() as client:
        assert client.post('/query', json=body()).status_code == 403
        assert not invoked
        r = client.post('/query', json=body(), headers={'X-API-Key': 'synthetic-api-key'})
    assert r.status_code == 200
    assert r.json()['usage_receipt']['query_id'] == 'q-api'
    assert len(authority.calls) == 1 and authority.finished == ['completed']


def test_missing_authority_is_rejected_before_retrieval_and_legacy_shape_is_unchanged(assembly):
    build, authority, invoked = assembly
    with build(False) as client:
        assert client.post('/query', json=body(), headers={'X-API-Key': 'synthetic-api-key'}).status_code == 503
        assert not invoked
        r = client.post('/query', json={'query': 'legacy question'}, headers={'X-API-Key': 'synthetic-api-key'})
    assert r.status_code == 200 and 'usage_receipt' not in r.json()
    assert not authority.calls


@pytest.mark.parametrize('path', ['/query/stream', '/query/data'])
def test_unaccounted_endpoint_cannot_silently_ignore_accounting(path, assembly):
    build, authority, invoked = assembly
    with build() as client:
        r = client.post(path, json=body(), headers={'X-API-Key': 'synthetic-api-key'})
    assert r.status_code == 422 and not invoked and not authority.calls
