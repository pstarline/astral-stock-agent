import json
from pathlib import Path

import pytest

from astral_stock_agent import cli


def test_duplicate_cash_cannot_hide_an_invalid_first_value(tmp_path, capsys):
    raw = (Path(__file__).parents[1] / 'examples/paper_snapshot.json').read_text()
    value = json.loads(raw)
    # Standard JSON decoding silently keeps the final, valid cash value.
    raw = raw.replace('"cash_usd":', '"cash_usd": "-1", "cash_usd":', 1)
    assert json.loads(raw) == value
    path = tmp_path / 'ambiguous.json'
    path.write_text(raw)
    assert cli.main([str(path)]) == 2
    assert capsys.readouterr().out == 'Report blocked: invalid input, configuration, or unconfirmed delivery\n'


@pytest.mark.parametrize('mode', [[], ['--preflight'], ['--account-observation'],
                                  ['--activity'], ['--reconcile'], ['--reconcile', '--send']])
@pytest.mark.parametrize('raw', [
    '{"source":"PRIVATE_FIRST","source":"fixture"}',
    '{"current":{"cash_usd":"PRIVATE_FIRST","cash_usd":"10"}}',
    '{"orders":[{"status":"PRIVATE_FIRST","status":"filled"}]}',
    '{"source":"PRIVATE_FIRST","so\\u0075rce":"fixture"}',
    '{"same":"PRIVATE_FIRST","same":"PRIVATE_FIRST"}',
])
def test_all_modes_reject_ambiguity_before_downstream_access(tmp_path, capsys, monkeypatch, mode, raw):
    def forbidden(*args, **kwargs):
        raise AssertionError('ambiguous input reached downstream processing')
    for name in ['render', 'normalize_preflight', 'render_account_observation',
                 'normalize_activity', 'reconcile_daily', 'Telegram', 'Outbox']:
        monkeypatch.setattr(cli, name, forbidden)
    path = tmp_path / 'ambiguous.json'
    path.write_text(raw)
    assert cli.main([str(path), *mode]) == 2
    output = capsys.readouterr().out
    assert output == 'Report blocked: invalid input, configuration, or unconfirmed delivery\n'
    assert 'PRIVATE_FIRST' not in output


def test_same_field_in_separate_objects_is_unambiguous(tmp_path, monkeypatch, capsys):
    snapshot = {'source': 'astral_mcp', 'orders': [{'quantity': '1'}, {'quantity': '2'}]}
    def render(value, **kwargs):
        assert value == snapshot
        return 'synthetic-key', 'synthetic preview'
    monkeypatch.setattr(cli, 'render', render)
    path = tmp_path / 'valid.json'
    path.write_text(json.dumps(snapshot))
    assert cli.main([str(path)]) == 0
    assert capsys.readouterr().out == 'synthetic preview\n'
