"""Reject invalid plaintext before ledger transitions or network I/O."""
import json
import sqlite3

import pytest

import astral_stock_agent.delivery as delivery
from astral_stock_agent.delivery import DeliveryError, Outbox, Telegram, validate_telegram_text
from test_reconciled_delivery import environment, invoke, rows


INVALID = ['', ' \n\t ', None, 42, b'bytes', 'x' * 4097, '\U0001f600' * 2049, '\ud800']


class Sender:
    def __init__(self):
        self.calls = []

    def send(self, body):
        self.calls.append(body)
        return '42'


@pytest.mark.parametrize('body', ['x', '한' * 4096, '\U0001f600' * 2048, 'x' * 4094 + '\U0001f600', '<b>plain text</b>\n'])
def test_supported_plaintext_boundaries_preserve_exact_body(tmp_path, body):
    validate_telegram_text(body)
    box = Outbox(tmp_path / 'reports.sqlite', create=True)
    sender = Sender()
    box.enqueue('day', body)
    assert box.deliver('day', sender)
    assert sender.calls == [body]


@pytest.mark.parametrize('body', INVALID)
def test_invalid_enqueue_does_not_create_record(tmp_path, body):
    box = Outbox(tmp_path / 'reports.sqlite', create=True)
    with pytest.raises(DeliveryError) as exc:
        box.enqueue('day', body)
    assert str(exc.value) == 'Telegram text invalid or above single-message limit'
    assert box.state('day') is None


@pytest.mark.parametrize('body', ['x' * 4097, '', '\U0001f600' * 2049])
def test_legacy_pending_record_stays_pending_without_attempt(tmp_path, body):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO reports VALUES ('day', ?, 'pending', NULL)", (body,))
    before = rows(path)
    sender = Sender()
    with pytest.raises(DeliveryError):
        box.deliver('day', sender)
    assert not sender.calls and rows(path) == before


@pytest.mark.parametrize('state', ['sending', 'unknown', 'sent'])
def test_preexisting_terminal_or_ambiguous_record_never_retried(tmp_path, state):
    path = tmp_path / 'reports.sqlite'
    box = Outbox(path, create=True)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO reports VALUES ('day', ?, ?, NULL)", ('x' * 4097, state))
    sender = Sender()
    assert not box.deliver('day', sender)
    assert not sender.calls and box.state('day') == state


@pytest.mark.parametrize('body', INVALID)
def test_direct_sender_rejects_before_constructing_request(monkeypatch, body):
    def forbidden(*args, **kwargs):
        pytest.fail('invalid body reached request construction or network transport')
    monkeypatch.setattr(delivery, 'Request', forbidden)
    monkeypatch.setattr(delivery, 'build_opener', forbidden)
    sender = Telegram('123:' + 'x' * 25, '42')
    with pytest.raises(DeliveryError):
        sender.send(body)


@pytest.mark.parametrize('body', ['x' * 4097, '\ud800'])
def test_cli_validation_precedes_sender_and_outbox_changes(environment, monkeypatch, body):
    import astral_stock_agent.cli as cli
    evidence, path, database, sender = environment
    Outbox(database).enqueue('preserved', 'private pending report')
    before = rows(database)
    # The real reconciler still checks the synthetic operational-shaped books;
    # replace only the final renderer to isolate the payload admission boundary.
    monkeypatch.setattr(cli, 'render', lambda *a, **kw: ('day', body))
    assert invoke(environment) == 2
    assert sender.constructed == 0 and not sender.calls
    assert rows(database) == before


def test_plaintext_request_adds_no_formatting_or_paid_broadcast_options(monkeypatch):
    captured = []
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def read(self, n):
            return b'{"ok":true,"result":{"message_id":42,"chat":{"id":42}}}'
    class Opener:
        def open(self, request, timeout):
            captured.append(json.loads(request.data))
            return Response()
    monkeypatch.setattr(delivery, 'build_opener', lambda *a: Opener())
    body = '<b>kept literal</b>\n한글'
    assert Telegram('123:' + 'x' * 25, '42').send(body) == '42'
    assert captured == [{'chat_id': '42', 'text': body}]
