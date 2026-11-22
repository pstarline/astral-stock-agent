"""Private write-once intent receipts, NEVER a dispatch queue or broker ledger.

Reuses the existing Paper ledger's checked POSIX primitives in a separate,
explicitly initialized directory. No Paper checkpoints are read or written.
Per-key O_EXCL prevents competing replacements; it does not lock account-wide
exposure or prove transmission/receipt/fills. Every receipt requires explicit
reconciliation and never authorizes submission, including a newly created one.
"""
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, DecimalException
import fcntl
from functools import wraps
import json
import os
import re

from .broker_order_state import OrderBook, OrderIntent, OrderRecord, _identifier
from .independent_ledger import IndependentPaperLedger, IndependentLedgerError, _canonical, _unique


ERROR = 'private intent journal unavailable; reconcile before proceeding'
MAX_INTENT_BYTES = 4096


class IntentJournalError(ValueError):
    pass


def _locked(method):
    @wraps(method)
    def guarded(self, *args, **kwargs):
        try:
            with self._exclusive_scope():
                return method(self, *args, **kwargs)
        except (OSError, ValueError, TypeError, DecimalException):
            raise IntentJournalError(ERROR) from None
    return guarded


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

    @contextmanager
    def _exclusive_scope(self):
        """Cooperating local processes only; never unlink/replace the lock inode.

        Nonblocking contention is a fail-closed result, not a retry instruction.
        All public read/record/inventory methods use this same immutable pin.
        Direct filesystem writers and different journals do not participate.
        """
        fd, acquired = None, False
        try:
            self._check_scope()  # Includes default public-CI and private-file checks.
            fd = os.open(self._pin, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            def verify():
                self._check_scope()
                opened, current = os.fstat(fd), self._pin.lstat()
                if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                    raise ValueError
            verify()
            yield
            verify()
        finally:
            if fd is not None:
                try:
                    if acquired:
                        fcntl.flock(fd, fcntl.LOCK_UN)
                finally:
                    os.close(fd)

    def _check_intent(self, intent, key):
        if ((intent.mode, intent.scope_digest) != (self._mode, self._scope)
                or intent.client_order_id != key):
            raise ValueError

    def _read_receipt(self, client_order_id) -> IntentReceipt:
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

    @_locked
    def read(self, client_order_id) -> IntentReceipt:
        return self._read_receipt(client_order_id)

    @_locked
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
                existing = self._read_receipt(intent.client_order_id).intent
                if existing != intent:
                    raise ValueError
                self._storage._sync(path)
                created = False
            verified = self._read_receipt(intent.client_order_id).intent
            if verified != intent:
                raise ValueError
            return IntentReceipt(verified, created)
        except (OSError, ValueError, TypeError, DecimalException):
            raise IntentJournalError(ERROR) from None

    def _inventory_names(self):
        self._check_scope()
        names = []
        for path in self._storage.directory.iterdir():
            if path.name == self._pin.name:
                continue
            if not re.fullmatch(r'[0-9a-f]{64}\.json', path.name) or len(names) >= 1024:
                raise ValueError
            names.append(path.name)
        # Repeated names can indicate a directory changed during enumeration.
        if len(set(names)) != len(names):
            raise ValueError
        return tuple(sorted(names))

    @_locked
    def load_unreconciled_book(self) -> OrderBook:
        """Read-only bounded two-pass reconstruction; NOT an atomic snapshot.

        Intents contain no broker outcomes. Every record is therefore unknown,
        never reserved/accepted/filled. Any other-symbol unknown also blocks the
        existing preflight. This call excludes cooperating journal methods only;
        direct filesystem writers still require external exclusion. The returned
        book is not fenced against an append AFTER this method releases its lock.
        Zero filled_quantity is only an unknown lower bound, not proof of no
        fills. Even an empty directory cannot prove historical completeness.
        """
        try:
            names = self._inventory_names()
            original, records = {}, []
            for name in names:
                path = self._storage.directory / name
                raw = self._storage._read(path)
                intent = _decode(raw)
                self._check_intent(intent, intent.client_order_id)
                if self._storage._path('broker-intent-v1:' + intent.client_order_id).name != name:
                    raise ValueError
                original[name] = raw
                records.append(OrderRecord(intent, status='unknown'))
            if self._inventory_names() != names:
                raise ValueError
            for name in names:
                if self._storage._read(self._storage.directory / name) != original[name]:
                    raise ValueError
            self._check_scope()
            return OrderBook(self._mode, self._scope, tuple(records))
        except (OSError, ValueError, TypeError, DecimalException):
            raise IntentJournalError(ERROR) from None
