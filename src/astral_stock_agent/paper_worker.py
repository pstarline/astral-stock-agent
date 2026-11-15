"""Private single-process daily Paper worker. No Astral or AI service calls."""
from datetime import date, datetime, timedelta, timezone
import json
import os
from pathlib import Path
import signal
from threading import Event
from zoneinfo import ZoneInfo

from .delivery import Telegram
from .independent_ledger import IndependentPaperLedger, _canonical, _unique
from .paper_runtime import run_market_paper
from .sessions import session_on


class WorkerError(ValueError):
    pass


def _check_control(control):
    """An incomplete prior attempt requires reconciliation across restarts."""
    for path in control.directory.iterdir():
        entry = json.loads(control._read(path), object_pairs_hook=_unique)
        if (set(entry) != {'kind', 'session'} or entry['kind'] not in ('attempt', 'complete')
                or date.fromisoformat(entry['session']).isoformat() != entry['session']
                or path != control._path(entry['kind'] + ':' + entry['session'])):
            raise ValueError
        peer_kind = 'complete' if entry['kind'] == 'attempt' else 'attempt'
        peer = control._path(peer_kind + ':' + entry['session'])
        if json.loads(control._read(peer)) != dict(kind=peer_kind, session=entry['session']):
            raise ValueError


def tick(environ, *, clock=None, runner=run_market_paper, sender_factory=Telegram):
    """Poll without requests until today's scheduled close plus 90 minutes.

    An explicit, externally approved candidate/configuration is required. Stores
    must already exist; missing volumes are never initialized on restart. An
    fsynced write-once attempt marker precedes provider I/O. A crash or failure
    blocks later work until the operator reconciles the private control store.
    """
    try:
        if os.getenv('GITHUB_ACTIONS') or environ.get('GITHUB_ACTIONS'):
            raise ValueError
        if environ.get('PAPER_WORKER_ENABLED', 'false') == 'false':
            return dict(status='disabled')
        if (environ.get('PAPER_WORKER_ENABLED') != 'true'
                or environ.get('PAPER_CANDIDATE_APPROVED') != 'true'
                or environ.get('PAPER_REPORTING_ENABLED', 'false') not in ('true', 'false')):
            raise ValueError
        now = (clock or (lambda: datetime.now(timezone.utc)))()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError
        session = session_on(now.astimezone(ZoneInfo('America/New_York')).date())
        root = Path(environ['PAPER_STORAGE_ROOT'])
        if not root.is_absolute():
            raise ValueError
        control = IndependentPaperLedger(root / 'control')
        _check_control(control)
        if session is None or now < session.closes_at + timedelta(minutes=90):
            return dict(status='waiting')
        day = session.day.isoformat()
        done = control._path('complete:' + day)
        if done.exists():
            return dict(status='already_completed', session=day)
        raw = environ['PAPER_CONFIG_JSON']
        if len(raw) > 1000000:
            raise ValueError
        config = json.loads(raw, object_pairs_hook=_unique)
        if config['symbol'] != environ['PAPER_US_SYMBOL']:
            raise ValueError
        bootstrap_day = date.fromisoformat(environ['PAPER_BOOTSTRAP_SESSION'])
        if bootstrap_day.isoformat() != environ['PAPER_BOOTSTRAP_SESSION'] or session.day < bootstrap_day:
            raise ValueError
        reporting = environ.get('PAPER_REPORTING_ENABLED', 'false') == 'true'
        sender = sender_factory(environ['TELEGRAM_BOT_TOKEN'], environ['TELEGRAM_CHAT_ID']) if reporting else None
        attempt = control._path('attempt:' + day)
        if not control._write_once(attempt, _canonical(dict(kind='attempt', session=day)), 'attempt conflict'):
            raise ValueError  # only the caller that created the marker may run
        result = runner(config, price_directory=root/'prices', paper_directory=root/'paper',
            database=root/'outbox'/'reports.sqlite', verified_us_symbols=[environ['PAPER_US_SYMBOL']],
            api_key=environ['ALPHAVANTAGE_API_KEY'], bootstrap=session.day == bootstrap_day,
            clock=clock, sender=sender, delivery_authorized=reporting)
        if reporting and result['state'] != 'sent':
            raise ValueError
        control._write_once(done, _canonical(dict(kind='complete', session=day)), 'completion conflict')
        return dict(status='completed', session=day, reporting_enabled=reporting,
                    recipient_receipt_verified=False)
    except Exception:
        raise WorkerError('private worker blocked; reconcile configuration, storage and delivery') from None


def main():
    """SIGTERM-aware idle loop; failure latches until explicit operator recovery."""
    stop = Event()
    def shutdown(*_):
        stop.set()
    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    halted = False
    previous = None
    while not stop.is_set():
        if not halted:
            try:
                result = tick(os.environ)
                if result != previous:
                    print(json.dumps(result, sort_keys=True), flush=True)
                previous = result
            except Exception:
                print('private worker halted; operator reconciliation required', flush=True)
                halted = True
        stop.wait(60)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
