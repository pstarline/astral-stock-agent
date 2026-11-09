"""Exercise the exact inline workflow without credentials or network access."""
from copy import deepcopy
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
