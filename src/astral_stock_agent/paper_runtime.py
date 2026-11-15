"""Single private Paper invocation; report delivery requires explicit opt-in."""
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path

from .delivery import Outbox
from .independent_ledger import IndependentPaperLedger, _canonical
from .market_data import fetch_alpha_daily, _scope
from .paper_pipeline import record_archived_sma, validate_record_config
from .paper_outbox import enqueue_latest_paper_report, report_latest_paper_once
from .price_archive import PriceArchive
from .sessions import latest_closed_session


class PaperRuntimeError(ValueError):
    pass


def run_market_paper(config, *, price_directory, paper_directory, database,
                     verified_us_symbols, api_key, bootstrap=False, clock=None,
                     sender=None, delivery_authorized=False):
    """Use caller-authorized configuration/key/storage for one candidate run.

    Validate static configuration and initialized private stores before the
    single official daily request. A completed same-session checkpoint skips
    collection/recalculation and safely repeats only its immutable queue handoff.
    Missing prior state fails closed. No secrets or prices are returned/logged.
    Delivery defaults off. An already-authorized caller must supply both a
    sender and delivery_authorized=True; only a newly inserted report is sent.
    This does not validate/approve a strategy or acquire operational authority.
    """
    try:
        if (os.getenv('GITHUB_ACTIONS') or type(delivery_authorized) is not bool
                or (delivery_authorized and not callable(getattr(sender, 'send', None)))
                or (not delivery_authorized and sender is not None)):
            raise ValueError
        clock = clock or (lambda:datetime.now(timezone.utc))
        started = clock()
        session = latest_closed_session(started)
        validate_record_config(config, symbol=config['symbol'], now=started, bootstrap=bootstrap)
        if bootstrap and config['rule']['window'] > 100:
            raise ValueError  # a compact bootstrap cannot supply this warmup
        _scope(config['symbol'], verified_us_symbols)
        roots = [Path(price_directory).absolute(), Path(paper_directory).absolute(),
                 Path(database).absolute().parent]
        if any(os.path.samefile(a,b) for i,a in enumerate(roots) for b in roots[i+1:]):
            raise ValueError
        PriceArchive(roots[0])
        paper = IndependentPaperLedger(roots[1])
        Outbox(database)
        pinned_path = paper._path('archived-sma-config-v1')
        try:
            pinned_path.lstat()
        except FileNotFoundError:
            if not bootstrap:
                raise ValueError
        else:
            if paper._read(pinned_path) != _canonical(config):
                raise ValueError
        checkpoint = paper._path('session:'+session.day.isoformat())
        try:
            checkpoint.lstat()
        except FileNotFoundError:
            if not bootstrap:
                previous = latest_closed_session(session.opens_at)
                paper._load_checkpoint(previous.day.isoformat())
            data = fetch_alpha_daily(symbol=config['symbol'], api_key=api_key,
                                    verified_us_symbols=verified_us_symbols, clock=clock)
            finished = clock()
            if (not timedelta(0) <= finished-started <= timedelta(minutes=1)
                    or latest_closed_session(finished).day != session.day):
                raise ValueError
            record_archived_sma(config,data,price_directory=roots[0],paper_directory=roots[1],
                verified_us_symbols=verified_us_symbols,now=finished,bootstrap=bootstrap)
            collected = True
        else:
            finished = clock()
            if latest_closed_session(finished).day != session.day or finished < started:
                raise ValueError
            collected = False
        if delivery_authorized:
            queued = report_latest_paper_once(paper_directory=roots[1], database=database,
                now=finished, sender=sender, delivery_authorized=True)
        else:
            queued = enqueue_latest_paper_report(paper_directory=roots[1],database=database,now=finished)
        return dict(session=session.day.isoformat(), collection_performed=collected,
                    candidate_only=True, recipient_receipt_verified=False, **queued)
    except Exception:
        raise PaperRuntimeError('private Paper run blocked; reconcile configuration/data/state') from None
