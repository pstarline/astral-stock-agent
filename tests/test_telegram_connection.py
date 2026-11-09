"""Mocked Bot API only: no real tokens, messages, personal data or network."""
import copy
import json
from urllib.error import HTTPError

import pytest

from astral_stock_agent import cli, delivery
from astral_stock_agent.delivery import DeliveryError, Outbox, Telegram


TOKEN = '123:' + 'synthetic' * 3
CHECK = {'bot_authentication_verified': True, 'private_chat_verified': True,
         'send_permission_verified': False, 'delivery_verified': False}


@pytest.fixture
def transport(monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    payloads = [{'ok': True, 'result': {'id': 123, 'is_bot': True, 'username': 'private-bot'}},
                {'ok': True, 'result': {'id': 42, 'type': 'private', 'first_name': 'private-person'}}]
    calls = []
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self, size):
            assert size == 65537
            payload = payloads[len(calls)-1]
            return payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    class Opener:
        def open(self, request, timeout):
            calls.append((request.full_url, json.loads(request.data)))
            assert timeout == 15 and request.get_method() == 'POST'
            return Response()
    def build(handler):
        assert isinstance(handler, delivery._NoRedirect)
        return Opener()
    monkeypatch.setattr(delivery, 'build_opener', build)
    return payloads, calls


def test_exact_readonly_methods_hide_private_data_and_do_not_consume_updates(transport):
    payloads, calls = transport; before = copy.deepcopy(payloads)
    assert Telegram(TOKEN, '42').check_connection() == CHECK
    assert calls == [(f'https://api.telegram.org/bot{TOKEN}/getMe', {}),
                     (f'https://api.telegram.org/bot{TOKEN}/getChat', {'chat_id': '42'})]
    assert payloads == before and 'private-' not in json.dumps(CHECK)


@pytest.mark.parametrize('change', [{'is_bot': 1}, {'is_bot': False}, {'id': True},
                                  {'id': 0}, {'id': '123'}, {'id': 2**52}])
def test_invalid_bot_stops_before_chat_lookup(transport, change):
    payloads, calls = transport; payloads[0]['result'].update(change)
    with pytest.raises(DeliveryError, match='^Telegram connection unverified$'):
        Telegram(TOKEN, '42').check_connection()
    assert len(calls) == 1


@pytest.mark.parametrize('change', [{'type': 'group'}, {'type': 'channel'}, {'id': 43},
                                  {'id': '42'}, {'id': True}])
def test_wrong_or_nonprivate_chat_is_rejected(transport, change):
    payloads, _ = transport; payloads[1]['result'].update(change)
    with pytest.raises(DeliveryError): Telegram(TOKEN, '42').check_connection()


@pytest.mark.parametrize('payload', [b'not-json', b'x' * 65537, None, [],
    {'ok': 1, 'result': {}}, {'ok': False, 'description': 'private-provider-error'},
    {'ok': True, 'result': []}])
def test_malformed_authentication_response_is_sanitized(transport, payload):
    payloads, calls = transport; payloads[0] = payload
    with pytest.raises(DeliveryError) as exc: Telegram(TOKEN, '42').check_connection()
    assert str(exc.value) == 'Telegram connection unverified' and len(calls) == 1


@pytest.mark.parametrize('failure', [TimeoutError(TOKEN),
    HTTPError('https://private-url', 429, 'private-description', {}, None)])
def test_network_errors_do_not_retry_or_disclose_urls(monkeypatch, transport, failure):
    calls = []
    class Broken:
        def open(self, *a, **kw): calls.append(1); raise failure
    monkeypatch.setattr(delivery, 'build_opener', lambda *a: Broken())
    with pytest.raises(DeliveryError) as exc: Telegram(TOKEN, '42').check_connection()
    assert str(exc.value) == 'Telegram connection unverified' and len(calls) == 1


@pytest.mark.parametrize('chat_id', ['-42', '@public-channel', '0', '042', True, 42,
                                   str(2**52), '9' * 5000])
def test_shared_sender_rejects_group_or_invalid_destinations_before_network(transport, chat_id):
    with pytest.raises(DeliveryError): Telegram(TOKEN, chat_id)
    assert transport[1] == []


def test_bot_cannot_be_its_own_recipient(transport):
    transport[0][1]['result']['id'] = 123
    with pytest.raises(DeliveryError): Telegram(TOKEN, '123').check_connection()


def test_cli_reads_private_settings_without_storage_or_message_access(monkeypatch, transport, capsys):
    monkeypatch.setenv('TELEGRAM_BOT_TOKEN', TOKEN)
    monkeypatch.setenv('TELEGRAM_CHAT_ID', '42')
    def forbidden(*a, **kw): pytest.fail('connection check attempted storage or send')
    monkeypatch.setattr(Telegram, 'send', forbidden)
    monkeypatch.setattr(Outbox, '__init__', forbidden)
    assert cli.main(['check-telegram']) == 0
    assert json.loads(capsys.readouterr().out) == CHECK


def test_public_ci_blocks_before_reading_secret_settings(monkeypatch, capsys):
    def guarded(name, *a):
        assert name == 'GITHUB_ACTIONS'
        return 'true'
    monkeypatch.setattr(cli.os, 'getenv', guarded)
    assert cli.main(['check-telegram']) == 2
    assert capsys.readouterr().out == 'Telegram check blocked: private configuration or connection unverified\n'


def test_direct_check_blocks_public_ci_before_network(monkeypatch, transport):
    monkeypatch.setenv('GITHUB_ACTIONS', 'true')
    with pytest.raises(DeliveryError): Telegram(TOKEN, '42').check_connection()
    assert transport[1] == []


def test_missing_settings_fail_locally_and_preserve_files(monkeypatch, transport, tmp_path, capsys):
    monkeypatch.delenv('TELEGRAM_BOT_TOKEN', raising=False)
    monkeypatch.delenv('TELEGRAM_CHAT_ID', raising=False)
    monkeypatch.chdir(tmp_path)
    assert cli.main(['check-telegram']) == 2
    assert transport[1] == [] and not list(tmp_path.iterdir())
    assert 'blocked' in capsys.readouterr().out


def test_no_commandline_secret_or_send_flags_are_accepted_or_echoed(transport, capsys):
    assert cli.main(['check-telegram', '--token', TOKEN, '--send']) == 2
    captured = capsys.readouterr()
    assert TOKEN not in captured.out + captured.err and not captured.err
    assert transport[1] == []


def test_chat_lookup_failure_stops_without_retry_or_send(transport):
    transport[0][1] = {'ok': False, 'description': 'private-chat-missing'}
    with pytest.raises(DeliveryError) as exc: Telegram(TOKEN, '42').check_connection()
    assert len(transport[1]) == 2 and 'private-' not in str(exc.value)


def test_bot_id_is_not_assumed_to_be_the_token_prefix(transport):
    transport[0][0]['result']['id'] = 789
    assert Telegram(TOKEN, '42').check_connection() == CHECK
