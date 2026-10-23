"""Accounting is opt-in, bounded and joined; the authority is an external boundary."""
import asyncio
import time
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lightrag.query_accounting import (
    QueryAccountingError,
    account_openai_call,
    query_usage_scope,
    retrieval_digest,
)


class Authority:
    def __init__(self):
        self.calls = {}
        self.finished = []

    async def claim(self, query_id, grant, request_digest, workspace):
        assert grant == 'synthetic-grant' and workspace == 'isolated-index'
        return {'query_id': query_id, 'lease_id': query_id + '-lease',
                'max_calls': 2, 'deadline': time.time() + 30,
                'models': [{'kind': kind, 'model': model, 'origin': 'https://model.invalid/v1',
                            'input_ceiling': 32768, 'output_ceiling': output}
                           for kind, model, output in [('llm', 'keyword-model', 100),
                                                       ('embedding', 'embedding-model', 0)]]}

    async def reserve_call(self, lease_id, call_id, **fields):
        assert call_id not in self.calls
        self.calls[call_id] = dict(lease_id=lease_id, **fields)

    async def settle_call(self, lease_id, call_id, usage):
        self.calls[call_id]['usage'] = usage

    async def finish(self, lease_id, status):
        self.finished.append(status)
        return {'query_id': lease_id.removesuffix('-lease'), 'state': 'settled' if status == 'completed' else status,
                'call_ids': list(self.calls)}


def client(retries=0):
    return SimpleNamespace(max_retries=retries, base_url='https://model.invalid/v1/')


@pytest.mark.asyncio
async def test_strict_clients_share_verified_trust_not_clients_or_authority(monkeypatch):
    import httpx
    import ssl
    from lightrag.query_accounting import openai_client_configs
    created = []
    def create(**kwargs):
        client = SimpleNamespace()
        created.append((client, kwargs))
        return client
    monkeypatch.setattr(httpx, 'AsyncClient', create)
    authority = Authority()
    async with query_usage_scope(authority, query_id='tls', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index'):
        first = openai_client_configs(None, 10)
        second = openai_client_configs(None, 10)
        assert first['http_client'] is not second['http_client']
        for _, kwargs in created:
            assert isinstance(kwargs.get('verify'), ssl.SSLContext)
            assert kwargs['verify'].verify_mode == ssl.CERT_REQUIRED and kwargs['verify'].check_hostname
            assert kwargs['follow_redirects'] is False and kwargs['trust_env'] is False
        assert created[0][1]['verify'] is created[1][1]['verify']
        assert first['max_retries'] == second['max_retries'] == 0
        with pytest.raises(QueryAccountingError): openai_client_configs({'max_retries': 1})
        with pytest.raises(QueryAccountingError): openai_client_configs({'http_client': object()})
    assert authority.calls == {}
    untouched = {'max_retries': 2}
    assert openai_client_configs(untouched) is untouched


def test_accounting_prewarm_loads_only_sdk_resources_and_trust(monkeypatch):
    import importlib
    import httpx
    import ssl
    from lightrag.query_accounting import prewarm_openai_accounting
    loaded = []
    monkeypatch.setattr(importlib, 'import_module', lambda name: loaded.append(name))
    def unexpected_client(**kwargs):
        raise AssertionError('prewarm must not construct or send a model client')
    monkeypatch.setattr(httpx, 'AsyncClient', unexpected_client)
    trust = prewarm_openai_accounting()
    assert loaded == ['openai.resources.chat.completions', 'openai.resources.embeddings']
    assert isinstance(trust, ssl.SSLContext)
    assert trust.verify_mode == ssl.CERT_REQUIRED and trust.check_hostname


@pytest.mark.asyncio
async def test_authority_pool_reuses_transport_not_headers_or_query_clients(monkeypatch):
    import httpx
    import ssl
    from lightrag.query_accounting import OpenAIAccountingTransport, openai_client_configs
    built, requests, closed = [], [], []
    class Transport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            requests.append((request.headers['authorization'], request.headers.get('cookie')))
            return httpx.Response(200, json={'ok': True}, headers={'Set-Cookie': 'private=first'})
        async def aclose(self): closed.append(True)
    def build(**kwargs):
        built.append(kwargs)
        return Transport()
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', build)
    authority = Authority()
    authority.openai_accounting_transport = OpenAIAccountingTransport(max_connections=20)
    clients = []
    try:
        for i in range(2):
            async with query_usage_scope(authority, query_id='pool-'+str(i), grant='synthetic-grant',
                    request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index'):
                client = openai_client_configs(None)['http_client']
                clients.append(client)
                async with client:
                    assert (await client.get('https://model.invalid/test',
                        headers={'Authorization': 'Bearer '+str(i)})).status_code == 200
            assert closed == []
        assert clients[0] is not clients[1] and requests == [('Bearer 0', None), ('Bearer 1', None)]
        assert len(built) == 1 and built[0]['retries'] == 0 and built[0]['trust_env'] is False
        assert built[0]['verify'].verify_mode == ssl.CERT_REQUIRED and built[0]['verify'].check_hostname
    finally:
        await authority.openai_accounting_transport.aclose()
    assert closed == [True]
    with pytest.raises(QueryAccountingError): authority.openai_accounting_transport.borrow()


@pytest.mark.asyncio
async def test_only_explicit_accounting_transport_can_be_borrowed():
    from lightrag.query_accounting import openai_client_configs
    authority = Authority()
    authority.openai_accounting_transport = object()
    async with query_usage_scope(authority, query_id='bad-pool', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index'):
        with pytest.raises(QueryAccountingError): openai_client_configs(None)


@pytest.mark.asyncio
async def test_host_can_extend_bounded_keepalive_without_retries(monkeypatch):
    import httpx
    from lightrag.query_accounting import OpenAIAccountingTransport
    options = []
    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self): pass
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kwargs: options.append(kwargs) or Transport())
    owner = OpenAIAccountingTransport(max_connections=20, keepalive_expiry=60)
    try:
        assert options[0]['retries'] == 0
        assert options[0]['limits'].max_connections == 20
        assert options[0]['limits'].max_keepalive_connections == 20
        assert options[0]['limits'].keepalive_expiry == 60
        for kwargs in ({'keepalive_expiry': True}, {'keepalive_expiry': None}, {'keepalive_expiry': 61}):
            with pytest.raises(ValueError): OpenAIAccountingTransport(max_connections=20, **kwargs)
        assert len(options) == 1, 'invalid host options must fail before a transport is created'
    finally:
        await owner.aclose()


@pytest.mark.asyncio
async def test_owner_pool_close_joins_physical_close_after_cancellation(monkeypatch):
    import httpx
    from lightrag.query_accounting import OpenAIAccountingTransport
    entered, release = asyncio.Event(), asyncio.Event()
    closed = []
    class Transport(httpx.AsyncBaseTransport):
        async def aclose(self):
            entered.set()
            await release.wait()
            closed.append(True)
    monkeypatch.setattr(httpx, 'AsyncHTTPTransport', lambda **kwargs: Transport())
    owner = OpenAIAccountingTransport(max_connections=2)
    task = asyncio.create_task(owner.aclose())
    try:
        await entered.wait()
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done() and not closed
        release.set()
        with pytest.raises(asyncio.CancelledError): await task
        await owner.aclose()
        assert closed == [True]
    finally:
        release.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize('second_action', ['closed', 'cancelled'])
async def test_pool_capacity_is_acquired_before_fresh_reservation_and_rechecked(second_action):
    from lightrag.query_accounting import OpenAIAccountingTransport
    authority = Authority()
    authority.openai_accounting_transport = OpenAIAccountingTransport(max_connections=1)
    entered, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    scopes = {}
    async def one(name, hold=False):
        async with query_usage_scope(authority, query_id=name, grant='synthetic-grant',
                request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index') as scope:
            scopes[name] = scope
            if name == 'second': second_ready.set()
            async def send():
                if hold:
                    entered.set()
                    await release.wait()
                return await response()
            return await account_openai_call('llm', client(), {'model': 'keyword-model'}, send)
    first = asyncio.create_task(one('first', True))
    second = None
    try:
        await entered.wait()
        second = asyncio.create_task(one('second'))
        await second_ready.wait()
        await asyncio.sleep(0)
        assert len(authority.calls) == 1 and not second.done()
        if second_action == 'closed': scopes['second'].closed = True
        else: second.cancel()
        release.set()
        await first
        with pytest.raises(QueryAccountingError if second_action == 'closed' else asyncio.CancelledError):
            await second
        assert len(authority.calls) == 1
        await one('third')
        assert len(authority.calls) == 2
    finally:
        release.set()
        await asyncio.gather(*(t for t in (first, second) if t is not None), return_exceptions=True)
        await authority.openai_accounting_transport.aclose()


async def response(model='keyword-model', usage=True):
    return SimpleNamespace(id='provider-id', model=model,
                           usage=SimpleNamespace(prompt_tokens=10, completion_tokens=2,
                                                 total_tokens=12) if usage else None)


@pytest.mark.asyncio
async def test_raw_usage_and_stable_calls_are_settled_before_query_receipt():
    authority = Authority()
    async with query_usage_scope(authority, query_id='q1', grant='synthetic-grant',
                                 request_digest=retrieval_digest('question', 'hybrid'),
                                 workspace='isolated-index') as scope:
        payload = {'model': 'keyword-model', 'messages': [{'role': 'user', 'content': 'question'}]}
        await account_openai_call('llm', client(), payload, lambda: response())
        assert payload['max_tokens'] == 100
        async def embed():
            return SimpleNamespace(model='embedding-model', id=None,
                                   usage=SimpleNamespace(prompt_tokens=3, total_tokens=3))
        await account_openai_call('embedding', client(), {'model': 'embedding-model', 'input': ['q']}, embed)
    assert len(authority.calls) == 2
    values = list(authority.calls.values())
    assert [v['usage']['prompt_tokens'] for v in values] == [10, 3]
    assert values[1]['usage']['completion_tokens'] == 0
    assert scope.receipt['state'] == 'settled'


@pytest.mark.asyncio
async def test_missing_usage_keeps_unknown_and_never_sends_a_retry():
    authority = Authority()
    sent = []
    async def unknown():
        sent.append(True)
        return await response(usage=False)
    async with query_usage_scope(authority, query_id='q2', grant='synthetic-grant',
                                 request_digest=retrieval_digest('question', 'hybrid'),
                                 workspace='isolated-index'):
        with pytest.raises(QueryAccountingError):
            await account_openai_call('llm', client(), {'model': 'keyword-model'}, unknown)
        with pytest.raises(QueryAccountingError):
            await account_openai_call('llm', client(), {'model': 'keyword-model'}, unknown)
    assert sent == [True]
    assert list(authority.calls.values())[0]['usage'] is None
    assert authority.finished == ['unknown']


def test_quote_validation_is_not_removed_by_python_optimization():
    result = subprocess.run([sys.executable, '-O', '-c', """
import time
from lightrag.query_accounting import _validate_quote, QueryAccountingError
try:
    _validate_quote({'query_id':'q', 'lease_id':'q', 'max_calls':0,
                     'deadline':time.time()+30, 'models':[]}, 'q')
except QueryAccountingError:
    raise SystemExit(0)
raise SystemExit(9)
"""], env=os.environ.copy(), capture_output=True)
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_client_retries_and_unmapped_models_are_rejected_before_send():
    authority = Authority()
    async def forbidden():
        raise AssertionError('must not send')
    async with query_usage_scope(authority, query_id='q3', grant='synthetic-grant',
                                 request_digest=retrieval_digest('question', 'naive'),
                                 workspace='isolated-index'):
        with pytest.raises(QueryAccountingError):
            await account_openai_call('llm', client(2), {'model': 'keyword-model'}, forbidden)
        with pytest.raises(QueryAccountingError):
            await account_openai_call('llm', client(), {'model': 'other-model'}, forbidden)
    assert not authority.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('choices', [2, 0, True, 1.0])
async def test_multiple_or_non_integer_choices_cannot_exceed_the_quote(choices):
    authority = Authority()
    sent = []

    async def wire():
        sent.append(True)
        return await response()

    async with query_usage_scope(authority, query_id='choice-bound', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index'):
        with pytest.raises(QueryAccountingError, match='query_usage_choice_limit'):
            await account_openai_call('llm', client(), {'model': 'keyword-model', 'n': choices}, wire)
    assert not sent and not authority.calls


@pytest.mark.asyncio
@pytest.mark.parametrize('closed_by', ['failed', 'deadline'])
async def test_reservation_return_rechecks_the_scope_before_wire(closed_by):
    authority = Authority()
    original_reserve = authority.reserve_call
    sent = []

    async def reserve(*args, **kwargs):
        await original_reserve(*args, **kwargs)
        if closed_by == 'failed':
            scope.failed = True
        else:
            scope.quote['deadline'] = time.time() - 1

    async def wire():
        sent.append(True)
        return await response()

    authority.reserve_call = reserve
    async with query_usage_scope(authority, query_id='reservation-tail', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index') as scope:
        with pytest.raises(QueryAccountingError):
            await account_openai_call('llm', client(), {'model': 'keyword-model'}, wire)
    assert not sent
    assert list(authority.calls.values())[0]['usage'] is None


@pytest.mark.asyncio
async def test_cancellation_waits_for_existing_usage_settlement():
    authority = Authority()
    sending, settling, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original = authority.settle_call
    async def settle(*args):
        settling.set()
        await release.wait()
        await original(*args)
    authority.settle_call = settle
    async def wire():
        sending.set()
        await asyncio.Event().wait()
    async def run():
        async with query_usage_scope(authority, query_id='q4', grant='synthetic-grant',
                                     request_digest=retrieval_digest('question', 'naive'),
                                     workspace='isolated-index'):
            await account_openai_call('llm', client(), {'model': 'keyword-model'}, wire)
    task = asyncio.create_task(run())
    await sending.wait()
    task.cancel()
    await settling.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert authority.finished == ['cancelled']
    assert list(authority.calls.values())[0]['usage'] is None


@pytest.mark.asyncio
async def test_existing_priority_workers_keep_each_query_usage_identity():
    from lightrag.utils import priority_limit_async_func_call
    authority = Authority()

    @priority_limit_async_func_call(2, max_queue_size=2, queue_name='accounting-contract')
    async def queued():
        await asyncio.sleep(0)
        return await account_openai_call('llm', client(), {'model': 'keyword-model'}, lambda: response())

    async def run(query_id):
        async with query_usage_scope(authority, query_id=query_id, grant='synthetic-grant',
                request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index'):
            await queued()
    try:
        await asyncio.gather(run('isolated-a'), run('isolated-b'))
    finally:
        await queued.shutdown()
    assert {v['lease_id'] for v in authority.calls.values()} == {'isolated-a-lease', 'isolated-b-lease'}
    assert all(v['usage']['prompt_tokens'] == 10 for v in authority.calls.values())


@pytest.mark.asyncio
async def test_scoped_provider_wire_cannot_repeat_inside_one_httpx_send(monkeypatch):
    import httpx
    from lightrag.query_accounting import openai_client_configs
    sent=[]
    async def reply(request):
        trace=request.extensions.get('trace')
        for _ in range(2):
            if trace:await trace('http2.send_request_headers.started',{})
            sent.append(True)
        return httpx.Response(200,json={'ok':True})
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:original(**kwargs,transport=httpx.MockTransport(reply)))
    authority=Authority()
    async with query_usage_scope(authority,query_id='wire-once',grant='synthetic-grant',
            request_digest=retrieval_digest('question','hybrid'),workspace='isolated-index'):
        configs=openai_client_configs(None,10)
        async with configs['http_client'] as http:
            with pytest.raises(QueryAccountingError):
                await http.post('https://model.invalid/v1/chat/completions',json={'model':'keyword-model'})
    assert sent==[True]


@pytest.mark.asyncio
@pytest.mark.parametrize('internal_replay', [False, True])
async def test_guarded_sdk_wire_preserves_exact_usage_or_original_unknown(monkeypatch, internal_replay):
    """从真实SDK经过guard到原费用收尾；不调用外部供应商。"""
    import httpx
    from openai import AsyncOpenAI
    from lightrag.query_accounting import openai_client_configs
    sent = []

    async def reply(request):
        trace = request.extensions['trace']
        for _ in range(2 if internal_replay else 1):
            await trace('http11.send_request_headers.started', {})
            sent.append(True)
        return httpx.Response(200, json={
            'id': 'synthetic-proof', 'object': 'chat.completion', 'model': 'keyword-model',
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': 'evidence'},
                         'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 10, 'completion_tokens': 2, 'total_tokens': 12}})

    class FixtureClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(**kwargs, transport=httpx.MockTransport(reply))
    monkeypatch.setattr(httpx, 'AsyncClient', FixtureClient)
    authority = Authority()
    async with query_usage_scope(authority, query_id='guarded-sdk', grant='synthetic-grant',
            request_digest=retrieval_digest('question', 'hybrid'), workspace='isolated-index') as scope:
        async with AsyncOpenAI(api_key='synthetic', base_url='https://model.invalid/v1',
                               **openai_client_configs(None, 10)) as sdk:
            payload = {'model': 'keyword-model', 'messages': [{'role': 'user', 'content': 'question'}]}
            async def send():
                return await sdk.chat.completions.create(**payload)
            if internal_replay:
                with pytest.raises(QueryAccountingError, match='query_usage_provider_outcome_unknown'):
                    await account_openai_call('llm', sdk, payload, send)
            else:
                result = await account_openai_call('llm', sdk, payload, send)
                assert result.choices[0].message.content == 'evidence'
    assert sent == [True]
    assert len(authority.calls) == 1
    call = next(iter(authority.calls.values()))
    assert call['usage'] is None if internal_replay else call['usage']['total_tokens'] == 12
    assert authority.finished == ['unknown' if internal_replay else 'completed']
    assert scope.receipt['state'] == ('unknown' if internal_replay else 'settled')
