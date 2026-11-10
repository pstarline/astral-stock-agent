"""One limited status run using caller-owned private connections and storage.

No authentication acquisition, scheduler, initialization or order authority.
This is an integration entry point, not an installed operational service.
"""
import asyncio
from datetime import datetime, timedelta, timezone
import os

from .account_observation import collect_account_observation
from .account_status import account_status_key, _render_observation
from .delivery import DeliveryError, Outbox
from .report import timestamp


COLLECTION_TIMEOUT_SECONDS = 30


def _result(state, attempted):
    if state not in ('pending', 'sending', 'sent', 'unknown'):
        raise DeliveryError()
    return {
        'state': state, 'new_attempt': attempted,
        'operator_reconciliation_required': state in ('pending', 'sending', 'unknown'),
        'recipient_receipt_verified': False, 'new_orders_allowed': False,
    }


async def run_account_status(call_tool, *, account_ref, strategy_ref, verified_us_symbols,
                             database, sender, clock=None):
    """Read official account/deployment data and attempt one daily private alert.

    An existing key in ANY state skips collection and delivery, including a
    pending row left by a crash. Pending/sending/unknown require the operator.
    Concurrent initial reads can overlap; the existing immutable enqueue and
    atomic delivery claim prevent duplicate sends. No ledger is auto-created.
    The caller supplies an already-authorized async MCP callback and sender.
    """
    try:
        if os.getenv('GITHUB_ACTIONS'):
            raise DeliveryError()
        clock = clock or (lambda: datetime.now(timezone.utc))
        started = clock()
        key = account_status_key('astral_mcp', account_ref, started)
        outbox = Outbox(database)
        state = outbox.state(key)
        if state is not None:
            return _result(state, False)
        if not callable(call_tool) or not callable(getattr(sender, 'send', None)):
            raise DeliveryError()
        observation = await asyncio.wait_for(collect_account_observation(
            call_tool, account_ref=account_ref, strategy_ref=strategy_ref,
            verified_us_symbols=verified_us_symbols, clock=clock), timeout=COLLECTION_TIMEOUT_SECONDS)
        finished = clock()
        if not timedelta(0) <= finished - started <= timedelta(minutes=5):
            raise DeliveryError()
        for field in ('checked_at', 'balance_received_at'):
            if not timedelta(0) <= finished - timestamp(observation[field]) <= timedelta(minutes=5):
                raise DeliveryError()
        final_key, body = _render_observation(observation, account_ref=account_ref, now=finished)
        # Do not silently move a run started before Korean midnight to tomorrow.
        if final_key != key or observation['source'] != 'astral_mcp':
            raise DeliveryError()
        outbox.enqueue(key, body)
        attempted = outbox.deliver(key, sender)
        return _result(outbox.state(key), attempted)
    except Exception:
        # Provider/SQLite/path/clock exceptions can contain account refs/secrets.
        # Outbox retains sending/unknown when the network result is uncertain.
        raise DeliveryError('private account status run blocked or delivery unconfirmed') from None
