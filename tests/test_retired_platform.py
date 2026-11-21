"""Current runtime admission: no historical-contract fixture is applied here."""
import asyncio
import importlib
import inspect

import pytest

from astral_stock_agent.retired_platform import RetiredPlatformError


@pytest.mark.parametrize('module,name', [
    ('activity','collect_activity'), ('activity','collect_session_activity'),
    ('preflight','collect_preflight'), ('account_observation','collect_account_observation'),
    ('status_runner','run_account_status'), ('public_discovery','run_public_discovery'),
    ('authoring','validate_sma_preview'),
])
@pytest.mark.parametrize('environment', ['normal', 'GITHUB_ACTIONS', 'PAPER_WORKER_ENABLED'])
def test_retired_callbacks_block_before_all_side_effects(module, name, environment, monkeypatch, tmp_path):
    if environment != 'normal': monkeypatch.setenv(environment, 'true')
    def forbidden(*args, **kwargs): raise AssertionError('side effect before policy check')
    target = getattr(importlib.import_module('astral_stock_agent.'+module), name)
    arguments = {key: forbidden for key, p in inspect.signature(target).parameters.items()
                 if p.default is inspect.Parameter.empty}
    if 'clock' in inspect.signature(target).parameters: arguments['clock'] = forbidden
    if module == 'public_discovery': arguments.update(enabled=True, mvp_verified=True)
    with pytest.raises(RetiredPlatformError, match='^retired platform execution is disabled$'):
        asyncio.run(target(**arguments))
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize('mode', [[], ['--account-status'], ['--reconcile']])
def test_retired_cli_send_blocks_before_snapshot_or_outbox_access(mode, monkeypatch, capsys):
    from pathlib import Path
    from astral_stock_agent.cli import main
    def forbidden(*args, **kwargs): raise AssertionError('private file touched')
    monkeypatch.setattr(Path, 'stat', forbidden)
    monkeypatch.setattr(Path, 'read_text', forbidden)
    assert main(['PRIVATE_SENTINEL', '--send', *mode]) == 2
    captured = capsys.readouterr()
    assert 'PRIVATE_SENTINEL' not in captured.out+captured.err
