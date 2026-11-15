"""Idempotent handoff of a stored independent Paper report; never sends."""
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
    try:
        if os.getenv('GITHUB_ACTIONS'):
            raise ValueError
        paper_path, outbox_parent = Path(paper_directory).absolute(), Path(database).absolute().parent
        if paper_path == outbox_parent or os.path.samefile(paper_path, outbox_parent):
            raise ValueError
        key, body = IndependentPaperLedger(paper_path).report_for_latest_session(now=now)
        box = Outbox(database)  # requires existing initialized private storage
        box.enqueue(key, body)
        state = box.state(key)
        if state not in ('pending', 'sending', 'unknown', 'sent'):
            raise ValueError
        return dict(report_present=True, state=state, delivery_attempted=False,
                    operator_reconciliation_required=state in ('pending', 'sending', 'unknown'))
    except Exception:
        raise PaperOutboxError('Paper report handoff blocked; reconcile private state') from None
