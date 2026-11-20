"""Archived daily inputs → pinned explicit candidate → private checkpoints.

No network, secrets, strategy approval, schedule or delivery authority. Prices
may be saved before an accounting failure; an identical retry reconciles them.
The two stores are not a distributed transaction and are never rolled back.
"""
from datetime import date
import os
from pathlib import Path

from .independent_ledger import IndependentPaperLedger, _canonical
from .independent_signals import preview_independent_sma
from .independent_reversal import preview_independent_reversal
from .market_data import CONFIG_FIELDS, RULE_FIELDS, SMA_RULE
from .price_archive import PriceArchive
from .research_execution import _decimal
from .research_reversal import validate_reversal_rule
from .sessions import latest_closed_session


class PaperPipelineError(ValueError):
    pass


def validate_record_config(config, *, symbol, now, bootstrap):
    """Check explicit candidate parameters before any market request or write."""
    try:
        if (type(bootstrap) is not bool or type(config) is not dict
                or set(config) != CONFIG_FIELDS or config['schema'] != 'archived_sma_record_v1'
                or config['symbol'] != symbol):
            raise ValueError
        rule = config['rule']
        if (type(rule) is not dict or set(rule) != RULE_FIELDS or rule['id'] != SMA_RULE
                or type(rule['window']) is not int or not 2 <= rule['window'] <= 252
                or _decimal(rule['units']) <= 0 or _decimal(config['initial_cash']) <= 0
                or not 0 <= _decimal(config['commission_bps']) < 10000
                or not 0 <= _decimal(config['slippage_bps']) < 10000):
            raise ValueError
        trade_from = date.fromisoformat(rule['trade_from'])
        if (trade_from.isoformat() != rule['trade_from']
                or (bootstrap and trade_from != latest_closed_session(now).day)):
            raise ValueError
    except Exception:
        raise PaperPipelineError('explicit private Paper configuration required') from None


def validate_reversal_record_config(config, *, symbol, now, bootstrap):
    try:
        if (type(bootstrap) is not bool or type(config) is not dict
                or set(config) != CONFIG_FIELDS or config['schema'] != 'archived_reversal_record_v1'
                or config['symbol'] != symbol or _decimal(config['initial_cash']) <= 0
                or not 0 <= _decimal(config['commission_bps']) < 10000
                or not 0 <= _decimal(config['slippage_bps']) < 10000):
            raise ValueError
        rule = validate_reversal_rule(config['rule'])
        if bootstrap and rule['trade_from'] != latest_closed_session(now).day:
            raise ValueError
    except Exception:
        raise PaperPipelineError('explicit private reversal configuration required') from None


def _candidate_contract(config):
    """Closed registry; no caller-supplied executable or source loading."""
    if type(config) is dict:
        if config.get('schema') == 'archived_sma_record_v1':
            return (validate_record_config, preview_independent_sma,
                    'independent_sma_candidate_v1', 'archived-sma-config-v1')
        if config.get('schema') == 'archived_reversal_record_v1':
            return (validate_reversal_record_config, preview_independent_reversal,
                    'independent_daily_reversal_v1', 'archived-reversal-config-v1')
    raise PaperPipelineError('explicit supported Paper candidate required')


def validate_candidate_config(config, *, symbol, now, bootstrap):
    _candidate_contract(config)[0](config, symbol=symbol, now=now, bootstrap=bootstrap)


def required_candidate_observations(config):
    """Call after configuration validation; do not request impossible warmup."""
    _candidate_contract(config)
    rule = config['rule']
    if config['schema'] == 'archived_sma_record_v1':
        return rule['window']
    return max(rule['ema_period'], rule['rsi_period'] + 1,
               rule['atr_period'] + rule['atr_mean_period'])


def check_candidate_pin(paper, config, *, bootstrap):
    """Check a legacy or new pin without writes; forbid adopting other state."""
    path = paper._path(_candidate_contract(config)[3])
    common = paper._path('archived-candidate-config-v1')
    common_present = False
    try:
        common.lstat()
    except FileNotFoundError:
        pass  # legacy SMA runs did not have the common candidate claim
    else:
        if paper._read(common) != _canonical(config):
            raise PaperPipelineError('candidate claim changed; reconcile private state')
        common_present = True
    for name in ('archived-sma-config-v1', 'archived-reversal-config-v1'):
        other = paper._path(name)
        if other == path:
            continue
        try:
            other.lstat()
        except FileNotFoundError:
            continue
        raise PaperPipelineError('foreign candidate state; reconcile before recording')
    try:
        path.lstat()
    except FileNotFoundError:
        # A crash between common claim and family pin can resume only the same
        # explicitly bootstrapped config, with no other existing state.
        leftovers = [p for p in paper.directory.iterdir() if not (common_present and p == common)]
        if not bootstrap or leftovers:
            raise PaperPipelineError('candidate pin missing; reconcile private state')
    else:
        if paper._read(path) != _canonical(config):
            raise PaperPipelineError('candidate pin changed; reconcile private state')
    return path


def record_archived_candidate(config, data, *, price_directory, paper_directory,
                              verified_us_symbols, now, bootstrap=False):
    """Record a candidate run with a flat, latest-session bootstrap baseline.

    The operator supplies all parameters; they are immutable after bootstrap.
    trade_from must be the bootstrap session, so earlier prices warm up inputs
    without backfilling trades. Final close signals execute at the next open.
    Returned metadata contains no prices, report text or account identifiers.
    """
    try:
        if os.getenv('GITHUB_ACTIONS') or type(bootstrap) is not bool:
            raise ValueError
        validator, preview, schema, _ = _candidate_contract(config)
        validator(config, symbol=data.symbol, now=now, bootstrap=bootstrap)
        if bootstrap and data.count < required_candidate_observations(config):
            raise ValueError
        prices_path, paper_path = Path(price_directory).absolute(), Path(paper_directory).absolute()
        if prices_path == paper_path or os.path.samefile(prices_path, paper_path):
            raise ValueError
        paper = IndependentPaperLedger(paper_path)
        config_path = check_candidate_pin(paper, config, bootstrap=bootstrap)
        pinned = _canonical(config)
        archive = PriceArchive(prices_path)
        # Freeze the candidate before any price snapshot/partial calculation.
        # An interrupted bootstrap may resume only this exact configuration.
        # One common claim also arbitrates two different candidate families
        # racing from the same empty directory. Keep family pins for legacy.
        paper._write_once(paper._path('archived-candidate-config-v1'), pinned,
                          'Paper candidate claim conflict')
        paper._write_once(config_path, pinned, 'Paper candidate configuration conflict')
        stored = archive.append(data, verified_us_symbols=verified_us_symbols, now=now,
                                bootstrap=bootstrap)
        spec = {k:config[k] for k in ('symbol','initial_cash','commission_bps','slippage_bps','rule')}
        spec.update(schema=schema, price_sha256=stored.csv_sha256,
                    price_count=stored.count, first_day=stored.first_day.isoformat(),
                    last_day=stored.last_day.isoformat(), label_convention=data.label_convention)
        _, accounting, body = preview(spec, stored.csv_bytes,
            verified_us_symbols=verified_us_symbols, now=now, allow_pending_only=True)
        if bootstrap and (accounting['cumulative_simulated_fills'] != 0
                          or _decimal(accounting['units']) != 0
                          or _decimal(accounting['cumulative_pnl']) != 0):
            raise ValueError
        result = paper.append(accounting, body, now=now, bootstrap=bootstrap)
        return dict(session=result['session'], created=result['created'],
                    price_history_created=stored.created, bootstrap=bootstrap,
                    candidate_only=True, delivery_attempted=False)
    except Exception:
        raise PaperPipelineError('archived Paper recording blocked; reconcile private state') from None


def record_archived_sma(config, data, **kwargs):
    """Legacy SMA-only entry point and immutable pin remain supported."""
    if type(config) is not dict or config.get('schema') != 'archived_sma_record_v1':
        raise PaperPipelineError('explicit SMA record configuration required')
    return record_archived_candidate(config, data, **kwargs)


def record_archived_reversal(config, data, **kwargs):
    """Explicit new candidate entry point; never switches an existing run."""
    if type(config) is not dict or config.get('schema') != 'archived_reversal_record_v1':
        raise PaperPipelineError('explicit reversal record configuration required')
    return record_archived_candidate(config, data, **kwargs)
