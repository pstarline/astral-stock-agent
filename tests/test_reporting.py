import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from astral_stock_agent.cli import main
from astral_stock_agent.delivery import DeliveryError, Outbox, Telegram
from astral_stock_agent.report import InvalidSnapshot, render


@pytest.fixture
def snapshot():
    return json.loads((Path(__file__).parents[1] / "examples/paper_snapshot.json").read_text())


def report(s):
    return render(s, now=datetime.fromisoformat(s["collected_at"].replace("Z", "+00:00")))


def test_fixture_is_labelled_and_unknown_is_not_zero(snapshot):
    _, body = report(snapshot)
    assert "실제 거래 기록 아님" in body
    assert "당일 체결: 없음" in body
    assert "당일 실현 손익: 미확인" in body


@pytest.mark.parametrize("field,value", [
    ("mode", "full_broker"), ("asset_class", "crypto"), ("currency", "EUR"),
    ("source", "untrusted"), ("schema_version", True), ("complete", False),
    ("complete", "true"), ("session_verified", False), ("cash_usd", "NaN"),
    ("cash_usd", "Infinity"), ("cash_usd", "-1"), ("cash_usd", 10000),
    ("cash_usd", "1e100"), ("strategy_name", "bad\ntext"),
    ("trading_day", "2026-11-07"), ("trading_day", "invalid"),
    ("collected_at", "2026-11-06T21:05:00"),
    ("session_closed_at", "2026-11-06T22:05:00Z"),
    ("valuation_at", "2026-11-05T21:00:00Z"), ("equity_usd", "9999"),
])
def test_unsafe_snapshots_rejected(snapshot, field, value):
    snapshot[field] = value
    with pytest.raises(InvalidSnapshot):
        report(snapshot)


@pytest.mark.parametrize("offset", [-1, 901])
def test_freshness(snapshot, offset):
    now = datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
    with pytest.raises(InvalidSnapshot):
        render(snapshot, now=now + timedelta(seconds=offset))


def filled(s):
    s["orders"] = [{"order_ref": "o1", "symbol": "SPY", "side": "buy", "status": "filled", "quantity": "2", "filled_quantity": "2"}]
    s["fills"] = [{"fill_ref": "f1", "order_ref": "o1", "quantity": "2", "price_usd": "500", "executed_at": "2026-11-06T20:00:00Z"}]
    return s


def test_actual_fill_rendered_only_after_reconciliation(snapshot):
    _, body = report(filled(snapshot))
    assert "체결 2주" in body
    assert "2주 @ $500.00" in body


@pytest.mark.parametrize("mutation", [
    lambda s: s["fills"].clear(),
    lambda s: s["fills"].append(copy.deepcopy(s["fills"][0])),
    lambda s: s["fills"][0].update(quantity="3"),
    lambda s: s["fills"][0].update(order_ref="orphan"),
    lambda s: s["fills"][0].update(executed_at="2026-11-05T20:00:00Z"),
    lambda s: s["orders"][0].update(status="accepted"),
    lambda s: s["orders"][0].update(status="made_up"),
    lambda s: s["orders"].append(copy.deepcopy(s["orders"][0])),
])
def test_bad_fill_ledger_rejected(snapshot, mutation):
    filled(snapshot)
    mutation(snapshot)
    with pytest.raises(InvalidSnapshot):
        report(snapshot)


def test_no_fill_does_not_become_execution(snapshot):
    filled(snapshot)
    snapshot["orders"][0].update(status="accepted", filled_quantity="0")
    snapshot["fills"] = []
    _, body = report(snapshot)
    assert "accepted" in body and "당일 체결: 없음" in body


def test_redeployment_and_changed_report_time_do_not_duplicate_key(snapshot):
    key, _ = report(snapshot)
    snapshot["deployment_ref"] = "new-run"
    snapshot["collected_at"] = "2026-11-06T21:06:00Z"
    assert report(snapshot)[0] == key
    snapshot["strategy_ref"] = "another-strategy"
    assert report(snapshot)[0] != key


class Sender:
    def __init__(self):
        self.calls = 0

    def send(self, body):
        self.calls += 1
        return "123"


def test_restart_prevents_duplicate(tmp_path):
    path = tmp_path / "reports.sqlite"
    box = Outbox(path)
    sender = Sender()
    box.enqueue("day", "original")
    assert box.deliver("day", sender)
    box = Outbox(path)
    box.enqueue("day", "changed")
    assert not box.deliver("day", sender)
    assert sender.calls == 1 and box.state("day") == "sent"


def test_concurrent_workers_only_one_attempt(tmp_path):
    path = tmp_path / "reports.sqlite"
    Outbox(path).enqueue("day", "report")
    sender = Sender()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: Outbox(path).deliver("day", sender), range(4)))
    assert sum(results) == 1 and sender.calls == 1


def test_timeout_and_crash_are_not_retried(tmp_path):
    box = Outbox(tmp_path / "reports.sqlite")

    class Timeout:
        def send(self, body):
            raise TimeoutError("secret URL must never escape")

    box.enqueue("timeout", "report")
    with pytest.raises(DeliveryError, match="automatic retry blocked"):
        box.deliver("timeout", Timeout())
    assert box.state("timeout") == "unknown"
    assert not box.deliver("timeout", Sender())

    class Crash:
        def send(self, body):
            raise SystemExit("process terminated")

    box.enqueue("crash", "report")
    with pytest.raises(SystemExit):
        box.deliver("crash", Crash())
    assert box.state("crash") == "sending"
    assert not box.deliver("crash", Sender())


def test_cli_fixture_cannot_send(tmp_path, snapshot, capsys):
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    assert main([str(path), "--send"]) == 2
    assert "Report blocked" in capsys.readouterr().out


def test_ci_cannot_send(tmp_path, snapshot, monkeypatch):
    snapshot["source"] = "astral_mcp"
    snapshot["collected_at"] = datetime.now().astimezone().isoformat()
    path = tmp_path / "snapshot.json"
    path.write_text(json.dumps(snapshot))
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    assert main([str(path), "--send", "--database", str(tmp_path / "x.sqlite")]) == 2
    assert not (tmp_path / "x.sqlite").exists()


def test_telegram_credentials_validated():
    with pytest.raises(DeliveryError):
        Telegram(None, None)
    with pytest.raises(DeliveryError):
        Telegram("123:" + "x" * 25, "@public_channel")


def test_telegram_contract_and_secret_redaction(monkeypatch):
    import astral_stock_agent.delivery as module
    token = "123:" + "x" * 25

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self, size):
            return b'{"ok":true,"result":{"message_id":12,"chat":{"id":42}}}'

    class Opener:
        def open(self, request, timeout):
            assert request.full_url == f"https://api.telegram.org/bot{token}/sendMessage"
            assert json.loads(request.data) == {"chat_id": "42", "text": "report"}
            assert timeout == 15
            return Response()

    monkeypatch.setattr(module, "build_opener", lambda *args: Opener())
    assert Telegram(token, "42").send("report") == "12"

    class Broken:
        def open(self, *args, **kwargs):
            raise OSError(token)

    monkeypatch.setattr(module, "build_opener", lambda *args: Broken())
    with pytest.raises(DeliveryError) as error:
        Telegram(token, "42").send("report")
    assert token not in str(error.value)
