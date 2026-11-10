"""Exercise the proposed fixed test with fake HTTP only; never read real secrets."""
import json
from pathlib import Path
import textwrap
from urllib.error import HTTPError

import pytest

WORKFLOW = Path(__file__).resolve().parents[1] / '.github/workflows/telegram-test.yml'


def source():
    return textwrap.dedent(WORKFLOW.read_text().split("          python3 - <<'PY'\n", 1)[1].rsplit('          PY', 1)[0])


@pytest.fixture
def environment(monkeypatch):
    for key, value in {
        'GITHUB_ACTIONS': 'true', 'GITHUB_EVENT_NAME': 'workflow_dispatch',
        'GITHUB_REPOSITORY': 'pstarline/astral-stock-agent',
        'GITHUB_REF': 'refs/heads/main', 'GITHUB_ACTOR': 'pstarline',
        'GITHUB_TRIGGERING_ACTOR': 'pstarline', 'GITHUB_RUN_ATTEMPT': '1',
        'TELEGRAM_BOT_TOKEN': '123456:' + 'x' * 30, 'TELEGRAM_CHAT_ID': '42',
    }.items():
        monkeypatch.setenv(key, value)


def run(monkeypatch, overrides=None, failure_at=None, failure=None, raw_send=None):
    import urllib.request
    scope = {'__name__': 'workflow_test'}
    exec(compile(source(), str(WORKFLOW), 'exec'), scope)
    values = {
        'getMe': {'id': 99, 'is_bot': True},
        'getChat': {'id': 42, 'type': 'private'},
        'sendMessage': {'message_id': 7, 'chat': {'id': 42, 'type': 'private'}, 'text': scope['MESSAGE']},
    }
    values.update(overrides or {})
    calls = []

    class Response:
        def __init__(self, raw):
            self.raw = raw
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
            assert timeout == 10 and method in values
            assert request.full_url.startswith('https://api.telegram.org/bot')
            calls.append((method, json.loads(request.data)))
            if method == failure_at:
                raise failure
            raw = raw_send if method == 'sendMessage' and raw_send is not None else json.dumps({'ok': True, 'result': values[method]}).encode()
            return Response(raw)

    monkeypatch.setattr(urllib.request, 'build_opener', lambda *_: Opener())
    try:
        exec(compile(source(), str(WORKFLOW), 'exec'), {'__name__': '__main__'})
    except SystemExit as error:
        assert error.code == 2
        return calls, False
    return calls, True


def test_only_one_fixed_message_after_private_destination_check(environment, monkeypatch, capsys):
    calls, success = run(monkeypatch)
    assert success and [c[0] for c in calls] == ['getMe', 'getChat', 'sendMessage']
    payload = calls[-1][1]
    assert payload['chat_id'] == 42 and payload['protect_content'] is True
    assert payload['text'] == ('[Astral 연결 점검]\n고정 테스트 알림입니다.\n'
                               '계좌 조회·배포·매매·손익 보고는 실행하지 않았습니다.\n'
                               '자동 일일 보고는 아직 비활성 상태입니다.')
    assert json.loads(capsys.readouterr().out) == {
        'telegram_api_accepted': True, 'recipient_receipt_verified': False,
    }


@pytest.mark.parametrize('key,value', [
    ('GITHUB_EVENT_NAME', 'pull_request'), ('GITHUB_REF', 'refs/heads/other'),
    ('GITHUB_ACTOR', 'other'), ('GITHUB_TRIGGERING_ACTOR', 'other'),
    ('GITHUB_REPOSITORY', 'other/astral-stock-agent'), ('GITHUB_ACTIONS', 'false'),
    ('GITHUB_RUN_ATTEMPT', '2'), ('GITHUB_RUN_ATTEMPT', ''),
    ('TELEGRAM_BOT_TOKEN', ''), ('TELEGRAM_BOT_TOKEN', 'PRIVATE_TOKEN'),
    ('TELEGRAM_CHAT_ID', ''), ('TELEGRAM_CHAT_ID', '-42'), ('TELEGRAM_CHAT_ID', '@name'),
    ('TELEGRAM_CHAT_ID', '0'), ('TELEGRAM_CHAT_ID', '0042'), ('TELEGRAM_CHAT_ID', str(2**52)),
])
def test_invalid_configuration_or_rerun_blocks_all_network(environment, monkeypatch, capsys, key, value):
    monkeypatch.setenv(key, value)
    calls, success = run(monkeypatch)
    output = capsys.readouterr()
    assert not success and calls == []
    assert 'PRIVATE' not in output.out + output.err and '123456' not in output.out
    assert '전송 전 중단' in output.out


@pytest.mark.parametrize('overrides,expected', [
    ({'getMe': {'id': 99, 'is_bot': False}}, ['getMe']),
    ({'getMe': {'id': 42, 'is_bot': True}}, ['getMe']),
    ({'getChat': {'id': 43, 'type': 'private'}}, ['getMe', 'getChat']),
    ({'getChat': {'id': 42, 'type': 'group'}}, ['getMe', 'getChat']),
    ({'getChat': {'id': '42', 'type': 'private'}}, ['getMe', 'getChat']),
    ({'getChat': []}, ['getMe', 'getChat']),
])
def test_unverified_destination_never_sends(environment, monkeypatch, overrides, expected):
    calls, success = run(monkeypatch, overrides=overrides)
    assert not success and [c[0] for c in calls] == expected


@pytest.mark.parametrize('failure', [
    TimeoutError('PRIVATE_TOKEN'), RuntimeError('PRIVATE_TOKEN'),
    HTTPError('https://api.telegram.org/botPRIVATE/sendMessage', 429, 'PRIVATE_REASON', {}, None),
])
def test_send_failure_is_unknown_without_retry_or_secret_output(environment, monkeypatch, capsys, failure):
    calls, success = run(monkeypatch, failure_at='sendMessage', failure=failure)
    output = capsys.readouterr()
    assert not success and [c[0] for c in calls] == ['getMe', 'getChat', 'sendMessage']
    assert '전송 결과 미확인' in output.out and '새 실행도 하지 마세요' in output.out
    assert 'PRIVATE' not in output.out + output.err and 'https://' not in output.out


@pytest.mark.parametrize('raw', [
    b'not json', b'[]', b'{"ok":false}', b'x' * 65537,
    b'{"ok":true,"result":{"message_id":true}}',
    b'{"ok":true,"result":{"message_id":7,"chat":{"id":43,"type":"private"}}}',
])
def test_uncertain_response_never_claims_acceptance_or_retries(environment, monkeypatch, capsys, raw):
    calls, success = run(monkeypatch, raw_send=raw)
    output = capsys.readouterr().out
    assert not success and len(calls) == 3
    assert 'telegram_api_accepted' not in output and '전송 결과 미확인' in output


def test_api_rejects_redirect_other_methods_and_variable_message():
    scope = {'__name__': 'workflow_test'}
    exec(compile(source(), str(WORKFLOW), 'exec'), scope)
    with pytest.raises(scope['CheckError']):
        scope['NoRedirect']().redirect_request(None, None, 302, '', {}, 'https://evil.invalid')
    for method, payload in [('getUpdates', {}), ('sendMessage', {'chat_id': 42, 'text': 'PRIVATE_TOKEN'})]:
        with pytest.raises(scope['CheckError']):
            scope['api']('private', method, payload)


def test_workflow_has_no_inputs_schedule_checkout_artifacts_or_credential_outputs():
    text = WORKFLOW.read_text()
    assert text.split('on:\n', 1)[1].split('permissions:', 1)[0] == '  workflow_dispatch:\n'
    assert 'permissions: {}' in text and 'uses:' not in text
    assert 'runs-on: ubuntu-24.04' in text and 'timeout-minutes: 2' in text
    assert "github.triggering_actor == 'pstarline' && github.run_attempt == 1" in text
    assert 'cancel-in-progress: false' in text and text.count('${{ secrets.') == 2
    assert 'GITHUB_OUTPUT' not in text and 'GITHUB_STEP_SUMMARY' not in text
    assert 'getUpdates' not in text and 'while ' not in text
