"""Exercise the exact inline workflow without credentials or network access."""
from copy import deepcopy
import json
from pathlib import Path
import textwrap
from types import SimpleNamespace

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/telegram-setup.yml'


@pytest.fixture
def script(monkeypatch):
    body = WORKFLOW.read_text().split("          python3 - <<'PY'\n", 1)[1]
    body = body.rsplit('          PY', 1)[0]
    scope = {'__name__': 'workflow_test'}
    exec(compile(textwrap.dedent(body), str(WORKFLOW), 'exec'), scope)
    scope['time'] = SimpleNamespace(time=lambda: 1000)
    for key, value in {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'pstarline/astral-stock-agent',
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_ACTOR': 'pstarline',
        'GITHUB_TRIGGERING_ACTOR': 'pstarline',
        'TELEGRAM_BOT_TOKEN': '123456:' + 'x' * 30,
    }.items():
        monkeypatch.setenv(key, value)
    return scope


def update(chat_id=42):
    return {'message': {'text': '/astral_setup', 'date': 995,
                        'from': {'id': chat_id, 'is_bot': False},
                        'chat': {'id': chat_id, 'type': 'private'}}}


def test_explicit_self_private_message(script):
    assert script['select_chat']([update(), update()], 'example_bot', 1000) == 42


@pytest.mark.parametrize('change', [
    {'date': 399}, {'date': 1001}, {'date': True}, {'text': '/start'},
    {'forward_origin': {}}, {'forward_from': {}}, {'via_bot': {}},
    {'sender_chat': {}}, {'business_connection_id': 'private'},
    {'from': {'id': 42, 'is_bot': True}},
    {'from': {'id': 43, 'is_bot': False}},
    {'chat': {'id': -42, 'type': 'group'}},
    {'chat': {'id': True, 'type': 'private'}},
])
def test_rejects_unintended_recipient(script, change):
    value = update()
    value['message'].update(change)
    with pytest.raises(script['SetupError']):
        script['select_chat']([value], 'example_bot', 1000)


@pytest.mark.parametrize('updates', [[], [update(), update(43)], [update()] * 100, {}, [None]])
def test_missing_ambiguous_or_incomplete_queue(script, updates):
    with pytest.raises(script['SetupError']):
        script['select_chat'](updates, 'example_bot', 1000)


def install_api(script, fail=None):
    calls = []
    responses = {
        'getWebhookInfo': {'url': ''},
        'getMe': {'id': 99, 'is_bot': True, 'username': 'example_bot'},
        'getUpdates': [update()],
        'sendMessage': {'message_id': 1, 'chat': {'id': 42, 'type': 'private'}},
    }
    def fake_api(token, method, payload):
        calls.append((method, deepcopy(payload)))
        if fail == method:
            raise TimeoutError('sensitive-url-token')
        return deepcopy(responses[method])
    script['api'] = fake_api
    return calls, responses


def test_only_private_setup_id_sent_no_queue_acknowledgement(script, capsys, monkeypatch):
    monkeypatch.setattr(script['time'], 'time', lambda: 1000)
    calls, _ = install_api(script)
    script['main']()
    assert [method for method, _ in calls] == ['getWebhookInfo', 'getMe', 'getUpdates', 'sendMessage']
    assert calls[2][1] == {'timeout': 0, 'limit': 100}
    sent = calls[3][1]
    assert sent['chat_id'] == 42
    assert 'TELEGRAM_CHAT_ID: 42' in sent['text']
    assert sent['protect_content'] is True
    assert 'parse_mode' not in sent
    output = capsys.readouterr().out
    assert '42' not in output and '123456' not in output and 'x' * 30 not in output


@pytest.mark.parametrize('key,value', [
    ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_EVENT_NAME', 'push'),
    ('GITHUB_REF', 'refs/heads/attacker'), ('GITHUB_ACTOR', 'stranger'),
    ('GITHUB_TRIGGERING_ACTOR', 'stranger'), ('GITHUB_ACTIONS', 'false'),
    ('GITHUB_REPOSITORY', 'stranger/astral-stock-agent'),
    ('TELEGRAM_BOT_TOKEN', ''), ('TELEGRAM_BOT_TOKEN', 'token/redirect'),
])
def test_runtime_guard_precedes_api(script, monkeypatch, key, value):
    calls, _ = install_api(script)
    monkeypatch.setenv(key, value)
    with pytest.raises(script['SetupError']):
        script['main']()
    assert calls == []


def test_existing_webhook_preserved_and_blocks_setup(script):
    calls, responses = install_api(script)
    responses['getWebhookInfo']['url'] = 'https://private.example/'
    with pytest.raises(script['SetupError']):
        script['main']()
    assert len(calls) == 1


@pytest.mark.parametrize('method', ['getWebhookInfo', 'getMe', 'getUpdates', 'sendMessage'])
def test_no_retry_on_failure_including_uncertain_send(script, method):
    calls, _ = install_api(script, method)
    with pytest.raises(TimeoutError):
        script['main']()
    assert sum(name == method for name, _ in calls) == 1


def test_redirects_never_followed(script):
    with pytest.raises(script['SetupError']):
        script['NoRedirect']().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')


@pytest.mark.parametrize('raw', [b'not-json', b'[]', b'{"ok": false}', b'{"ok": 1, "result": {}}', b'x' * 65537])
def test_api_rejects_untrusted_responses(script, raw):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            assert size == 65537
            return raw
    class Opener:
        def open(self, req, timeout):
            assert req.full_url.startswith('https://api.telegram.org/bot')
            assert timeout == 10 and req.method == 'POST'
            return Response()
    script['build_opener'] = lambda *_: Opener()
    with pytest.raises((script['SetupError'], ValueError)):
        script['api']('123456:' + 'x' * 30, 'getMe', {})


def test_entrypoint_suppresses_sensitive_exceptions(script, monkeypatch, capsys):
    import urllib.request
    def fail(*args, **kwargs):
        raise RuntimeError('TOKEN-SHOULD-NEVER-APPEAR')
    monkeypatch.setattr(urllib.request, 'build_opener', fail)
    body = WORKFLOW.read_text().split("          python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0]
    with pytest.raises(SystemExit) as caught:
        exec(compile(textwrap.dedent(body), str(WORKFLOW), 'exec'), {'__name__': '__main__'})
    assert caught.value.code == 2
    output = capsys.readouterr()
    assert 'TOKEN' not in output.out + output.err
    assert '자동 재시도하지 않습니다' in output.out


def test_workflow_has_no_automatic_trigger_checkout_or_extra_secret():
    text = WORKFLOW.read_text()
    assert text.split('on:\n', 1)[1].split('permissions:', 1)[0] == '  workflow_dispatch:\n'
    assert 'permissions: {}' in text
    assert 'uses:' not in text and 'checkout' not in text.lower().replace('# no checkout', '')
    assert text.count('${{ secrets.') == 1
    assert 'secrets.TELEGRAM_BOT_TOKEN' in text
    assert 'GITHUB_OUTPUT' not in text and 'GITHUB_STEP_SUMMARY' not in text
    assert 'api.github.com' not in text
    assert 'timeout-minutes: 2' in text


def run_entrypoint(monkeypatch, responses=None, failure=None):
    """Run the exact entrypoint with fake Telegram responses, including its error handler."""
    import time
    import urllib.request
    values = {
        'getWebhookInfo': {'url': ''},
        'getMe': {'id': 99, 'is_bot': True, 'username': 'example_bot'},
        'getUpdates': [update()],
        'sendMessage': {'message_id': 1, 'chat': {'id': 42, 'type': 'private'}},
    }
    values.update(responses or {})
    calls = []
    class Response:
        def __init__(self, result):
            self.raw = json.dumps({'ok': True, 'result': result}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return self.raw[:size]
    class Opener:
        def open(self, request, timeout):
            method = request.full_url.rsplit('/', 1)[1]
            calls.append(method)
            if failure and method == failure[0]:
                raise failure[1]
            return Response(values[method])
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    monkeypatch.setattr(time, 'time', lambda: 1000)
    body = WORKFLOW.read_text().split("          python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0]
    scope = {'__name__': '__main__'}
    try:
        exec(compile(textwrap.dedent(body), str(WORKFLOW), 'exec'), scope)
    except SystemExit as error:
        assert error.code == 2
        return calls, False
    return calls, True


@pytest.mark.parametrize('token,expected', [('', '비어 있습니다'), ('"secret"', '토큰 형식이 잘못됐습니다')])
def test_secret_failure_has_actionable_diagnostic_without_api(script, monkeypatch, capsys, token, expected):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', token)
    calls, success = run_entrypoint(monkeypatch)
    output = capsys.readouterr().out
    assert not success and calls == []
    assert expected in output and '진단 단계: secret' in output and '전송 시도: 없음' in output
    assert '"secret"' not in output


def test_copy_paste_surrounding_whitespace_is_accepted(script, monkeypatch, capsys):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', '  123456:' + 'x' * 30 + '\n')
    calls, success = run_entrypoint(monkeypatch)
    assert success and calls[-1] == 'sendMessage'
    assert '123456' not in capsys.readouterr().out


@pytest.mark.parametrize('status,expected', [
    (401, '인증 실패'), (403, '접근 거부'), (404, '토큰 전체'),
    (409, '충돌'), (429, '요청 제한'), (500, 'HTTP 오류'),
])
def test_http_failure_never_prints_sensitive_url_or_body(script, monkeypatch, capsys, status, expected):
    from urllib.error import HTTPError
    failure = HTTPError('https://api.telegram.org/botPRIVATE_TOKEN/getWebhookInfo', status, 'PRIVATE_REASON', {}, None)
    calls, success = run_entrypoint(monkeypatch, failure=('getWebhookInfo', failure))
    output = capsys.readouterr().out
    assert not success and calls == ['getWebhookInfo']
    assert expected in output and '전송 시도: 없음' in output
    assert 'PRIVATE' not in output and 'https://' not in output


@pytest.mark.parametrize('responses,expected', [
    ({'getWebhookInfo': {'url': 'https://PRIVATE_WEBHOOK'}}, '기존 웹훅'),
    ({'getUpdates': []}, '개인 메시지를 찾지 못했습니다'),
    ({'getUpdates': [update(), update(43)]}, '여러 개인 대화'),
    ({'getUpdates': [update()] * 100}, '대기 메시지 상한'),
])
def test_recipient_block_has_specific_diagnostic_and_no_send(script, monkeypatch, capsys, responses, expected):
    calls, success = run_entrypoint(monkeypatch, responses=responses)
    output = capsys.readouterr().out
    assert not success and 'sendMessage' not in calls
    assert expected in output and '전송 시도: 없음' in output
    assert 'PRIVATE' not in output


def test_send_timeout_is_uncertain_and_never_retried(script, monkeypatch, capsys):
    calls, success = run_entrypoint(monkeypatch, failure=('sendMessage', TimeoutError('PRIVATE_TOKEN')))
    output = capsys.readouterr().out
    assert not success and calls.count('sendMessage') == 1
    assert '진단 단계: send' in output and '전송 시도: 있음' in output
    assert '대조하기 전 재실행하지 마세요' in output
    assert 'PRIVATE' not in output
