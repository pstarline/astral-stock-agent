"""Verify the exact manual read-only workflow with no real credentials/network."""
import json
from pathlib import Path
import textwrap
from urllib.error import HTTPError

import pytest


WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/telegram-check.yml'


def source():
    return textwrap.dedent(WORKFLOW.read_text().split("          python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0])


@pytest.fixture
def environment(monkeypatch):
    for key, value in {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'pstarline/astral-stock-agent',
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_ACTOR': 'pstarline',
        'GITHUB_TRIGGERING_ACTOR': 'pstarline',
        'TELEGRAM_BOT_TOKEN': '123456:' + 'x' * 30, 'TELEGRAM_CHAT_ID': '42',
    }.items():
        monkeypatch.setenv(key, value)


def run(monkeypatch, destination=None, failure=None, bot=None):
    import urllib.request
    calls = []
    values = {'getMe': bot if bot is not None else {'id': 99, 'is_bot': True},
              'getChat': destination if destination is not None else {'id': 42, 'type': 'private'}}
    class Response:
        def __init__(self, value):
            self.raw = json.dumps({'ok': True, 'result': value}).encode()
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            assert size == 65537
            return self.raw
    class Opener:
        def open(self, request, timeout):
            method = request.full_url.rsplit('/', 1)[1]
            payload = json.loads(request.data)
            calls.append((method, payload))
            assert method in ('getMe', 'getChat') and timeout == 10
            if failure:
                raise failure
            return Response(values[method])
    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    try:
        exec(compile(source(), str(WORKFLOW), 'exec'), {'__name__': '__main__'})
    except SystemExit as error:
        assert error.code == 2
        return calls, False
    return calls, True


def test_reads_exact_private_id_without_sending(environment, monkeypatch, capsys):
    calls, success = run(monkeypatch)
    assert success and calls == [('getMe', {}), ('getChat', {'chat_id': 42})]
    assert json.loads(capsys.readouterr().out) == {
        'bot_authenticated': True, 'private_chat_verified': True,
        'send_permission_verified': False, 'delivery_verified': False,
    }


@pytest.mark.parametrize('key,value', [
    ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/other'),
    ('GITHUB_ACTOR', 'other'), ('GITHUB_TRIGGERING_ACTOR', 'other'),
    ('GITHUB_REPOSITORY', 'other/astral-stock-agent'), ('GITHUB_ACTIONS', 'false'),
    ('TELEGRAM_BOT_TOKEN', ''), ('TELEGRAM_BOT_TOKEN', 'PRIVATE_TOKEN'),
    ('TELEGRAM_CHAT_ID', ''), ('TELEGRAM_CHAT_ID', '-42'), ('TELEGRAM_CHAT_ID', '@name'),
    ('TELEGRAM_CHAT_ID', '0'), ('TELEGRAM_CHAT_ID', 'True'),
    ('TELEGRAM_CHAT_ID', '0042'), ('TELEGRAM_CHAT_ID', str(2**52)),
])
def test_bad_context_or_config_blocks_network(environment, monkeypatch, capsys, key, value):
    monkeypatch.setenv(key, value)
    calls, success = run(monkeypatch)
    output = capsys.readouterr().out
    assert not success and calls == []
    assert 'PRIVATE_TOKEN' not in output and '123456' not in output


@pytest.mark.parametrize('destination', [
    {'id': 43, 'type': 'private'}, {'id': 42, 'type': 'group'},
    {'id': True, 'type': 'private'}, {'id': '42', 'type': 'private'}, {}, [],
])
def test_different_or_nonprivate_destination_rejected(environment, monkeypatch, capsys, destination):
    calls, success = run(monkeypatch, destination=destination)
    assert not success and len(calls) == 2
    assert '설정 ID와 개인 대화가 일치하지 않습니다' in capsys.readouterr().out


@pytest.mark.parametrize('failure', [
    HTTPError('https://api.telegram.org/botPRIVATE/getMe', 401, 'PRIVATE_REASON', {}, None),
    HTTPError('https://api.telegram.org/botPRIVATE/getMe', 400, 'PRIVATE_REASON', {}, None),
    TimeoutError('PRIVATE_TOKEN'), RuntimeError('PRIVATE_TOKEN'),
])
def test_errors_never_leak_details_or_retry(environment, monkeypatch, capsys, failure):
    calls, success = run(monkeypatch, failure=failure)
    output = capsys.readouterr()
    assert not success and calls == [('getMe', {})]
    assert 'PRIVATE' not in output.out + output.err and 'https://' not in output.out


def test_nonbot_identity_blocks_destination(environment, monkeypatch, capsys):
    calls, success = run(monkeypatch, bot={'id': 99, 'is_bot': False})
    assert not success and calls == [('getMe', {})]


def test_redirect_rejected_and_mutating_method_refused():
    scope = {'__name__': 'check_test'}
    exec(compile(source(), str(WORKFLOW), 'exec'), scope)
    with pytest.raises(scope['CheckError']):
        scope['NoRedirect']().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')
    with pytest.raises(scope['CheckError']):
        scope['api']('private', 'sendMessage', {})


def test_workflow_only_manual_with_no_checkout_artifacts_or_writes():
    text = WORKFLOW.read_text()
    assert text.split('on:\n', 1)[1].split('permissions:', 1)[0] == '  workflow_dispatch:\n'
    assert 'permissions: {}' in text and 'uses:' not in text
    assert text.count('${{ secrets.') == 2
    assert 'getUpdates' not in text and 'sendMessage' not in text
    assert 'GITHUB_OUTPUT' not in text and 'GITHUB_STEP_SUMMARY' not in text
