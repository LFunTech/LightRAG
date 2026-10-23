"""Actual OpenAI provider paths, with only the SDK network boundary replaced."""
from types import SimpleNamespace
import pytest

from lightrag.query_accounting import query_usage_scope, retrieval_digest
from tests.llm.test_query_accounting import Authority


@pytest.mark.asyncio
async def test_keyword_and_embedding_providers_report_raw_usage_before_return(monkeypatch):
    from lightrag.llm import openai as module
    sent = []

    class Client:
        max_retries = 0
        base_url = 'https://model.invalid/v1/'

        def __init__(self):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.complete))
            self.embeddings = SimpleNamespace(create=self.embed)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def close(self):
            pass

        async def complete(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(model='keyword-model', id='provider-llm',
                usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2, total_tokens=12),
                choices=[SimpleNamespace(message=SimpleNamespace(content='keywords'), finish_reason='stop')])

        async def embed(self, **kwargs):
            sent.append(kwargs)
            return SimpleNamespace(model='embedding-model', id='provider-embedding',
                usage=SimpleNamespace(prompt_tokens=3, total_tokens=3),
                data=[SimpleNamespace(embedding=[0.1, 0.2])])

    monkeypatch.setattr(module, 'create_openai_async_client', lambda **kwargs: Client())
    authority = Authority()
    async with query_usage_scope(authority, query_id='q-provider', grant='synthetic-grant',
                                 request_digest=retrieval_digest('question', 'hybrid'),
                                 workspace='isolated-index'):
        assert await module.openai_complete_if_cache('keyword-model', 'question', api_key='synthetic') == 'keywords'
        vector = await module.openai_embed.func(['question'], model='embedding-model', api_key='synthetic',
                                              max_token_size=0)
        assert vector.shape == (1, 2)
    assert len(authority.calls) == 2, 'provider calls escaped the query authority'
    assert [v['usage']['prompt_tokens'] for v in authority.calls.values()] == [10, 3]
    assert sent[0]['max_tokens'] == 100


@pytest.mark.asyncio
async def test_real_client_factory_disables_hidden_retry_redirect_and_proxy():
    from lightrag.llm.openai import create_openai_async_client
    authority = Authority()
    async with query_usage_scope(authority, query_id='transport', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'naive'), workspace='isolated-index'):
        client = create_openai_async_client(api_key='synthetic', base_url='https://model.invalid/v1')
        try:
            assert client.max_retries == 0 and client._client.follow_redirects is False
            assert client._client._trust_env is False
        finally:
            await client.close()
