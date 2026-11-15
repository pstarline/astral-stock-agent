"""Private Paper handoff and explicitly authorized first-attempt delivery."""
import os
from pathlib import Path

from .delivery import Outbox
from .independent_ledger import IndependentPaperLedger


class PaperOutboxError(ValueError):
    pass


def enqueue_latest_paper_report(*, paper_directory, database, now):
    """Validate private checkpoint first, then atomically insert/compare Outbox.

    Existing pending/sending/unknown/sent states and bodies remain immutable.
    A crash after insertion is safe to repeat with the same stored report.
    This handoff never calls deliver, loads secrets or constructs Telegram.
    The checkpoint and Outbox are separate stores, not one transaction.
    """
    return _handoff(paper_directory=paper_directory, database=database, now=now)


def report_latest_paper_once(*, paper_directory, database, now, sender,
                             delivery_authorized=False):
    """For an already-approved private caller; this flag is not an approval.

    Attempt only the report inserted by THIS invocation. All preexisting rows,
    including pending, are preserved without sending. A crash after insertion
    sacrifices delivery until operator reconciliation, rather than duplicates.
    No credentials are acquired and no candidate is approved by this function.
    """
    try:
        if (os.getenv('GITHUB_ACTIONS') or delivery_authorized is not True
                or not callable(getattr(sender, 'send', None))):
            raise ValueError
    except Exception:
        raise PaperOutboxError('explicit private report delivery authorization required') from None
    return _handoff(paper_directory=paper_directory, database=database, now=now, sender=sender)


def _handoff(*, paper_directory, database, now, sender=None):
    try:
        if os.getenv('GITHUB_ACTIONS'):
            raise ValueError
        paper_path, outbox_parent = Path(paper_directory).absolute(), Path(database).absolute().parent
        if paper_path == outbox_parent or os.path.samefile(paper_path, outbox_parent):
            raise ValueError
        key, body = IndependentPaperLedger(paper_path).report_for_latest_session(now=now)
        box = Outbox(database)  # requires existing initialized private storage
        inserted = box.enqueue(key, body)
        attempted = bool(sender is not None and inserted and box.deliver(key, sender))
        state = box.state(key)
        if state not in ('pending', 'sending', 'unknown', 'sent'):
            raise ValueError
        return dict(report_present=True, state=state, delivery_attempted=attempted,
                    operator_reconciliation_required=state in ('pending', 'sending', 'unknown'))
    except Exception:
        raise PaperOutboxError('Paper report handoff blocked; reconcile private state') from None
