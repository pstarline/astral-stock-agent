"""Archived daily inputs → pinned SMA candidate → private Paper checkpoints.

No network, secrets, strategy approval, schedule or delivery authority. Prices
may be saved before an accounting failure; an identical retry reconciles them.
The two stores are not a distributed transaction and are never rolled back.
"""
from datetime import date
import os
from pathlib import Path

from .independent_ledger import IndependentPaperLedger, _canonical
from .independent_signals import preview_independent_sma
from .market_data import CONFIG_FIELDS, RULE_FIELDS, SMA_RULE
from .price_archive import PriceArchive
from .research_execution import _decimal
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


def record_archived_sma(config, data, *, price_directory, paper_directory,
                        verified_us_symbols, now, bootstrap=False):
    """Record a candidate run with a flat, latest-session bootstrap baseline.

    The operator supplies all parameters; they are immutable after bootstrap.
    trade_from must be the bootstrap session, so earlier prices warm up the SMA
    without backfilling trades. Final close signals execute at the next open.
    Returned metadata contains no prices, report text or account identifiers.
    """
    try:
        if os.getenv('GITHUB_ACTIONS') or type(bootstrap) is not bool:
            raise ValueError
        validate_record_config(config, symbol=data.symbol, now=now, bootstrap=bootstrap)
        prices_path, paper_path = Path(price_directory).absolute(), Path(paper_directory).absolute()
        if prices_path == paper_path or os.path.samefile(prices_path, paper_path):
            raise ValueError
        paper = IndependentPaperLedger(paper_path)
        config_path = paper._path('archived-sma-config-v1')
        pinned = _canonical(config)
        if not bootstrap and paper._read(config_path) != pinned:
            raise ValueError
        archive = PriceArchive(prices_path)
        stored = archive.append(data, verified_us_symbols=verified_us_symbols, now=now,
                                bootstrap=bootstrap)
        spec = {k:config[k] for k in ('symbol','initial_cash','commission_bps','slippage_bps','rule')}
        spec.update(schema='independent_sma_candidate_v1', price_sha256=stored.csv_sha256,
                    price_count=stored.count, first_day=stored.first_day.isoformat(),
                    last_day=stored.last_day.isoformat(), label_convention=data.label_convention)
        _, accounting, body = preview_independent_sma(spec, stored.csv_bytes,
            verified_us_symbols=verified_us_symbols, now=now, allow_pending_only=True)
        if bootstrap and (accounting['cumulative_simulated_fills'] != 0
                          or _decimal(accounting['units']) != 0
                          or _decimal(accounting['cumulative_pnl']) != 0):
            raise ValueError
        paper._write_once(config_path, pinned, 'Paper candidate configuration conflict')
        result = paper.append(accounting, body, now=now, bootstrap=bootstrap)
        return dict(session=result['session'], created=result['created'],
                    price_history_created=stored.created, bootstrap=bootstrap,
                    candidate_only=True, delivery_attempted=False)
    except Exception:
        raise PaperPipelineError('archived Paper recording blocked; reconcile private state') from None
