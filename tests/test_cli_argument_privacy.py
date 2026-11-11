"""Synthetic mistakenly pasted configuration must never enter CLI diagnostics."""
from contextlib import contextmanager
import os
from pathlib import Path
import subprocess
import sys

import pytest

import astral_stock_agent.cli as cli


COMMANDS = [
    ['private-input.json', '--account-status', '--send', '--database', 'private.sqlite'],
    ['inspect-outbox', '--database', 'private.sqlite'],
    ['init-outbox', '--database', 'private.sqlite'],
    ['audit-research', 'manifest.json', 'prices.csv', '--us-symbol', 'SPY'],
    ['check-telegram'],
    ['preview-simulation', 'manifest.json', 'prices.csv', '--us-symbol', 'SPY'],
    ['preview-sma', 'candidate.json', 'prices.csv', '--us-symbol', 'SPY'],
    ['preview-market-sma', 'candidate.json', '--us-symbol', 'IBM'],
    ['record-simulation', 'manifest.json', 'prices.csv', '--ledger-directory', 'ledger',
     '--us-symbol', 'SPY'],
]
MARKER = 'SYNTHETIC_PRIVATE_VALUE'


@contextmanager
def no_operational_access(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Argument rejection must precede operational access')
    with monkeypatch.context() as patch:
        for name in ['Telegram', 'Outbox', 'ClosingBooks', 'render', 'reconcile_daily']:
            patch.setattr(cli, name, forbidden)
        for name in ['stat', 'open', 'read_text']:
            patch.setattr(Path, name, forbidden)
        yield


@pytest.mark.parametrize('command', COMMANDS)
@pytest.mark.parametrize('suffix', [
    ['--bot-token', MARKER], ['--bot-token=' + MARKER],
    ['--' + MARKER], [MARKER],
    ['--bot-token', 'https://example.invalid/' + MARKER],
])
def test_invalid_arguments_never_echo_values_flags_or_urls_or_touch_runtime(
        monkeypatch, capsys, command, suffix):
    with no_operational_access(monkeypatch):
        assert cli.main([*command, *suffix]) == 2
    output = capsys.readouterr()
    assert MARKER not in output.out + output.err
    assert 'https://' not in output.out + output.err
    assert 'blocked' in (output.out + output.err).lower()


@pytest.mark.parametrize('arguments', [
    [], ['inspect-outbox'], ['init-outbox'], ['audit-research'],
    ['private-input.json', '--database'], ['inspect-outbox', '--database'],
])
def test_missing_required_arguments_are_safe_status_two(monkeypatch, capsys, arguments):
    with no_operational_access(monkeypatch):
        assert cli.main(arguments) == 2
    output = capsys.readouterr()
    assert 'blocked' in (output.out + output.err).lower()
    assert 'Traceback' not in output.err


@pytest.mark.parametrize('command', [[], ['inspect-outbox'], ['init-outbox'],
                                     ['audit-research'], ['check-telegram'], ['preview-simulation'],
                                     ['preview-sma'], ['preview-market-sma'], ['record-simulation']])
def test_help_still_works_without_runtime_access(monkeypatch, capsys, command):
    with no_operational_access(monkeypatch):
        with pytest.raises(SystemExit) as result:
            cli.main([*command, '--help'])
    assert result.value.code == 0
    output = capsys.readouterr()
    assert 'usage:' in output.out and output.err == ''


@pytest.mark.parametrize('command', COMMANDS[:3] + [c for c in COMMANDS if c[0] in ('preview-sma', 'preview-market-sma', 'record-simulation')])
def test_actual_module_process_exits_two_without_argument_reflection(command):
    root = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [sys.executable, '-m', 'astral_stock_agent.cli', *command, '--bot-token', MARKER],
        env={**os.environ, 'PYTHONPATH': str(root / 'src')},
        capture_output=True, text=True, timeout=15, cwd=root,
    )
    assert result.returncode == 2
    assert result.stdout == ''
    assert result.stderr == 'Command blocked: invalid arguments; use --help\n'
    assert MARKER not in result.stdout + result.stderr

