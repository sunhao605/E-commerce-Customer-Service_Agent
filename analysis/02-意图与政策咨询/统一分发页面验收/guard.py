"""模型 HTTP 计量与预算闸门。只允许聊天补全端点；计量覆盖 httpx/httpx2 同步与异步。"""
import asyncio
import hashlib
import importlib
import json
import time
from contextlib import ExitStack
from unittest.mock import patch
from urllib.parse import urlparse


class Guard:
    def __init__(self, *, model, max_calls, token_stop, case_budgets, log_path, case_getter, phase_getter,
                 local_prefixes=()):
        self.model = model
        self.max_calls = max_calls
        self.token_stop = token_stop
        self.case_budgets = case_budgets
        self.log_path = log_path
        self.case_getter = case_getter
        self.phase_getter = phase_getter
        self.local_prefixes = tuple(local_prefixes)
        self.items = []
        self.stop = None

    def permitted(self, request):
        url = urlparse(str(request.url))
        return (request.method == 'POST' and url.scheme == 'https' and url.hostname == 'api.deepseek.com'
                and url.port in (None, 443) and url.path == '/chat/completions')

    def local_business(self, request):
        return any(str(request.url).startswith(prefix) for prefix in self.local_prefixes)

    def used_tokens(self):
        return sum(item.get('usage', {}).get('total_tokens', 0) for item in self.items)

    def halt(self, reason):
        self.stop = reason
        raise RuntimeError(reason)

    def before_send(self, request):
        if self.local_business(request):
            return None
        case = self.case_getter()
        if self.stop:
            raise RuntimeError('run_stopped:' + self.stop)
        if not self.permitted(request):
            self.halt('network_destination_or_method_blocked')
        if len(self.items) >= self.max_calls:
            self.halt('total_call_budget_reached')
        if self.used_tokens() >= self.token_stop:
            self.halt('token_budget_reached')
        budget = self.case_budgets.get(case)
        if budget is not None and sum(item['case'] == case for item in self.items) >= budget:
            self.halt('case_call_budget_reached')
        body = json.loads(request.content)
        if body.get('model') != self.model or body.get('stream'):
            self.halt('unexpected_model_or_stream')
        item = {'case': case, 'stage': self.phase_getter(), 'model': body.get('model'),
                'status': 'started', 'input_sha256': hashlib.sha256(request.content).hexdigest()}
        self.items.append(item)
        return item

    def capture(self, item, response, started):
        item.update(status=response.status_code, elapsed_ms=round((time.monotonic() - started) * 1000))
        try:
            payload = response.json()
        except (ValueError, UnicodeDecodeError):
            payload = {}
        item['usage'] = {key: value for key, value in (payload.get('usage') or {}).items()
                         if type(value) is int and value >= 0}
        try:
            choice = payload['choices'][0]
            item['raw_content'] = str(choice.get('message', {}).get('content', ''))[:4000]
            item['raw_sha256'] = hashlib.sha256(str(choice.get('message', {}).get('content', '')).encode()).hexdigest()
            item['finish_reason'] = choice.get('finish_reason')
        except (KeyError, IndexError, TypeError):
            item['raw_content'] = ''
        if response.status_code in {401, 402, 403, 429}:
            self.stop = 'provider_http_' + str(response.status_code)
        self.write(item)

    def write(self, item):
        with open(self.log_path, 'a') as stream:
            stream.write(json.dumps(item, ensure_ascii=False) + '\n')

    def wrap_sync(self, original):
        def send(client, request, *args, **kwargs):
            item = self.before_send(request)
            if item is None:
                return original(client, request, *args, **kwargs)
            started = time.monotonic()
            try:
                response = original(client, request, *args, **kwargs)
                response.read()
                self.capture(item, response, started)
                return response
            except Exception as exc:
                item.update(status=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000))
                self.write(item)
                raise
        return send

    def wrap_async(self, original):
        async def send(client, request, *args, **kwargs):
            item = self.before_send(request)
            if item is None:
                return await original(client, request, *args, **kwargs)
            started = time.monotonic()
            try:
                response = await original(client, request, *args, **kwargs)
                await response.aread()
                self.capture(item, response, started)
                return response
            except Exception as exc:
                item.update(status=type(exc).__name__, elapsed_ms=round((time.monotonic() - started) * 1000))
                self.write(item)
                raise
        return send

    def install(self, stack):
        for name in ('httpx', 'httpx2'):
            module = importlib.import_module(name)
            stack.enter_context(patch.object(module.Client, '_send_single_request', self.wrap_sync(module.Client._send_single_request)))
            stack.enter_context(patch.object(module.AsyncClient, '_send_single_request', self.wrap_async(module.AsyncClient._send_single_request)))


def self_check():
    """无外呼验证主机/方法/总预算/单例预算/usage 与原始输出采集。"""
    import httpx
    budgets = {'a': 1}
    guard = Guard(model='deepseek-flash', max_calls=3, token_stop=300000, case_budgets=budgets,
                  log_path='/dev/null', case_getter=lambda: 'a', phase_getter=lambda: 'self_check')
    with ExitStack() as stack:
        guard.install(stack)
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json={
            'usage': {'total_tokens': 1}, 'choices': [{'message': {'content': '{"ok":1}'}, 'finish_reason': 'stop'}]}))
        with httpx.Client(transport=transport) as client:
            client.post('https://api.deepseek.com/chat/completions', json={'model': 'deepseek-flash'})
            for method, url in [('GET', 'https://api.deepseek.com/chat/completions'),
                                ('POST', 'https://other.invalid/chat/completions'),
                                ('POST', 'http://127.0.0.1:8081/api/orders')]:
                try:
                    client.request(method, url, json={'model': 'deepseek-flash'})
                except RuntimeError:
                    pass
                else:
                    raise AssertionError('forbidden_request_not_blocked')
            try:
                client.post('https://api.deepseek.com/chat/completions', json={'model': 'deepseek-flash'})
            except RuntimeError:
                pass
            else:
                raise AssertionError('case_budget_not_enforced')

        guard.stop = None
        guard.case_budgets = {'a': 9}

        async def async_call():
            async with httpx.AsyncClient(transport=transport) as client:
                await client.post('https://api.deepseek.com/chat/completions', json={'model': 'deepseek-flash'})
        asyncio.run(async_call())
    assert len(guard.items) == 2 and all(item['status'] == 200 for item in guard.items)
    assert guard.items[0]['raw_content'] == '{"ok":1}' and guard.items[0]['usage'] == {'total_tokens': 1}
    # 单例预算与总预算都在下一请求前拦截。
    guard.stop = None
    guard.case_budgets = {'a': 9}
    guard.max_calls = 2
    try:
        guard.before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions',
                                        json={'model': 'deepseek-flash'}))
    except RuntimeError as exc:
        assert 'total_call_budget_reached' in str(exc)
    else:
        raise AssertionError('total_budget_not_enforced')
    guard.stop = None
    guard.max_calls = 10
    guard.token_stop = 1
    try:
        guard.before_send(httpx.Request('POST', 'https://api.deepseek.com/chat/completions',
                                        json={'model': 'deepseek-flash'}))
    except RuntimeError as exc:
        assert 'token_budget_reached' in str(exc)
    else:
        raise AssertionError('token_budget_not_enforced')
    return {'checks_passed': 7, 'real_http': 0}
