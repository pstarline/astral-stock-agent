"""Private write-once intent receipts, NEVER a dispatch queue or broker ledger.

Reuses the existing Paper ledger's checked POSIX primitives in a separate,
explicitly initialized directory. No Paper checkpoints are read or written.
Per-key O_EXCL prevents competing replacements; it does not lock account-wide
exposure or prove transmission/receipt/fills. Every receipt requires explicit
reconciliation and never authorizes submission, including a newly created one.
"""
from dataclasses import asdict, dataclass, field
from decimal import Decimal, DecimalException
import json

from .broker_order_state import OrderBook, OrderIntent, _identifier
from .independent_ledger import IndependentPaperLedger, IndependentLedgerError, _canonical, _unique


ERROR = 'private intent journal unavailable; reconcile before proceeding'
MAX_INTENT_BYTES = 4096


class IntentJournalError(ValueError):
    pass


@dataclass(frozen=True)
class IntentReceipt:
    intent: OrderIntent = field(repr=False)
    created: bool

    @property
    def reconciliation_required(self):
        return True

    @property
    def order_submission_approved(self):
        return False


def _reject_constant(value):
    raise ValueError


def _encode(intent):
    if type(intent) is not OrderIntent or len(str(intent.limit_price)) > 128:
        raise ValueError
    value = asdict(intent)
    value['limit_price'] = str(intent.limit_price)
    raw = _canonical(dict(schema='private_broker_intent_v1', intent=value))
    if len(raw) > MAX_INTENT_BYTES:
        raise ValueError
    return raw


def _decode(raw):
    if not 0 < len(raw) <= MAX_INTENT_BYTES:
        raise ValueError
    value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
    if (type(value) is not dict or set(value) != {'schema', 'intent'}
            or value['schema'] != 'private_broker_intent_v1'):
        raise ValueError
    item = value['intent']
    if (type(item) is not dict or set(item) != {'mode', 'scope_digest', 'client_order_id',
            'symbol', 'side', 'quantity', 'limit_price'}
            or type(item['limit_price']) is not str or len(item['limit_price']) > 128):
        raise ValueError
    item['limit_price'] = Decimal(item['limit_price'])
    return OrderIntent(**item)


class PrivateIntentJournal:
    """No implicit bootstrap on restart, repair, deletion, retries or migration.

    Scope is a caller-supplied private namespace digest, not account identity
    authentication. Different directories are NOT globally deduplicated. All
    returned intent objects/asdict are private, not sanitized public output.
    """

    def __init__(self, directory, *, mode, scope_digest, initialize=False):
        try:
            if type(initialize) is not bool:
                raise ValueError
            OrderBook(mode, scope_digest)  # Reuse strict namespace validation.
            self._mode, self._scope = mode, scope_digest
            self._storage = IndependentPaperLedger(directory)
            self._pin = self._storage._path('broker-intent-journal-scope-v1')
            self._pin_bytes = _canonical(dict(schema='private_broker_intent_scope_v1',
                                             mode=mode, scope_digest=scope_digest))
            if initialize:
                # A nonempty foreign/partial directory must not be adopted.
                if not self._pin.exists() and next(self._storage.directory.iterdir(), None) is not None:
                    raise ValueError
                self._storage._write_once(self._pin, self._pin_bytes, ERROR)
            self._check_scope()
        except (OSError, ValueError, TypeError):
            raise IntentJournalError(ERROR) from None

    def _check_scope(self):
        if self._storage._read(self._pin) != self._pin_bytes:
            raise ValueError

    def _check_intent(self, intent, key):
        if ((intent.mode, intent.scope_digest) != (self._mode, self._scope)
                or intent.client_order_id != key):
            raise ValueError

    def read(self, client_order_id) -> IntentReceipt:
        try:
            if not _identifier(client_order_id):
                raise ValueError
            self._check_scope()
            path = self._storage._path('broker-intent-v1:' + client_order_id)
            intent = _decode(self._storage._read(path))
            self._check_intent(intent, client_order_id)
            return IntentReceipt(intent, False)
        except (OSError, ValueError, TypeError, DecimalException):
            raise IntentJournalError(ERROR) from None

    def record(self, intent: OrderIntent) -> IntentReceipt:
        try:
            raw = _encode(intent)
            self._check_intent(intent, intent.client_order_id)
            self._check_scope()
            path = self._storage._path('broker-intent-v1:' + intent.client_order_id)
            try:
                created = self._storage._write_once(path, raw, ERROR)
            except IndependentLedgerError:
                # Same numerical price with a different decimal spelling is a
                # no-op too. Preserve the FIRST bytes; never rewrite the file.
                existing = self.read(intent.client_order_id).intent
                if existing != intent:
                    raise ValueError
                self._storage._sync(path)
                created = False
            verified = self.read(intent.client_order_id).intent
            if verified != intent:
                raise ValueError
            return IntentReceipt(verified, created)
        except (OSError, ValueError, TypeError, DecimalException):
            raise IntentJournalError(ERROR) from None
