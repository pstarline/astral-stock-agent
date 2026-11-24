"""Private immutable observation EVIDENCE, never a recovered broker ledger.

Only caller-supplied original book/query can decode a record. No API, account,
clock, preflight, intent-journal unlocking or order dispatch is connected here.
Successful reads do not prove a prior write acknowledgment or source authenticity.
"""
import base64
from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
from functools import wraps
from hashlib import sha256
import json
import os

from .broker_observation_codec import (
    MAX_BYTES, _reject_constant, decode_observation_pages, encode_observation_pages,
)
from .broker_order_state import OrderBook, _identifier
from .independent_ledger import IndependentPaperLedger, _canonical, _unique


ERROR = 'private observation store unavailable; reconciliation required'
MAX_RECORDS = 32
MAX_ENVELOPE_BYTES = 700000


class ObservationStoreError(ValueError):
    pass


def _guarded(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        try:
            with self._exclusive():
                return method(self, *args, **kwargs)
        except (OSError, ValueError, TypeError, RecursionError):
            raise ObservationStoreError(ERROR) from None
    return call


@dataclass(frozen=True)
class StoredObservation:
    pages: tuple = field(repr=False)
    created: bool

    @property
    def reconciliation_required(self):
        return True

    @property
    def order_submission_approved(self):
        return False


class PrivateObservationStore:
    """Explicit empty-directory bootstrap; no mkdir, repair, cleanup or retries.

    At most 32 immutable records. A nonblocking directory-inode flock coordinates
    this store's methods only; it does NOT lock other stores or account exposure.
    Existing Paper/Outbox/control/intent directories must never be used here.
    All returned pages/asdict/bytes remain private. No book is reconstructed.
    """

    def __init__(self, directory, *, mode, scope_digest, initialize=False):
        try:
            if type(initialize) is not bool:
                raise ValueError
            OrderBook(mode, scope_digest)
            self._mode, self._scope = mode, scope_digest
            self._storage = IndependentPaperLedger(directory)
            self._pin = self._storage._path('broker-observation-store-scope-v1')
            self._pin_bytes = _canonical(dict(schema='private_observation_scope_v1',
                                             mode=mode, scope_digest=scope_digest))
            if initialize:
                if not self._pin.exists() and next(self._storage.directory.iterdir(), None) is not None:
                    raise ValueError
                self._storage._write_once(self._pin, self._pin_bytes, ERROR)
            with self._exclusive():
                self._inventory()
        except (OSError, ValueError, TypeError, RecursionError):
            raise ObservationStoreError(ERROR) from None

    def _check(self):
        if self._storage._read(self._pin) != self._pin_bytes:
            raise ValueError

    @contextmanager
    def _exclusive(self):
        self._check()
        fd = os.open(self._storage.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        acquired = False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
            def verify():
                self._check()
                info = os.fstat(fd)
                if (info.st_dev, info.st_ino) != self._storage._identity:
                    raise ValueError
            verify()
            yield
            verify()
        finally:
            try:
                if acquired:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _path(self, key):
        if not _identifier(key):
            raise ValueError
        return self._storage._path('broker-observation-v1:' + key)

    def _unpack(self, raw):
        if not 0 < len(raw) <= MAX_ENVELOPE_BYTES:
            raise ValueError
        value = json.loads(raw, object_pairs_hook=_unique, parse_constant=_reject_constant)
        if (type(value) is not dict or set(value) != {
                'schema', 'mode', 'scope_digest', 'evidence_id', 'observation_b64', 'payload_sha256'}
                or value['schema'] != 'private_observation_evidence_v1'
                or (value['mode'], value['scope_digest']) != (self._mode, self._scope)
                or not _identifier(value['evidence_id'])
                or type(value['observation_b64']) is not str):
            raise ValueError
        payload = base64.b64decode(value['observation_b64'], validate=True)
        if not 0 < len(payload) <= MAX_BYTES or sha256(payload).hexdigest() != value['payload_sha256']:
            raise ValueError
        # Other records' baseline books are deliberately NOT reconstructed.
        return value['evidence_id'], payload

    def _inventory(self):
        self._check()
        records = {}
        for path in self._storage.directory.iterdir():
            if path == self._pin:
                continue
            if len(records) >= MAX_RECORDS:
                raise ValueError
            key, payload = self._unpack(self._storage._read(path))
            if path != self._path(key) or key in records:
                raise ValueError
            records[key] = payload
        return records

    def _decode(self, payload, book, query_digest):
        if type(book) is not OrderBook or (book.mode, book.scope_digest) != (self._mode, self._scope):
            raise ValueError
        return decode_observation_pages(payload, book=book, query_digest=query_digest)

    @_guarded
    def read(self, evidence_id, *, book, query_digest):
        path = self._path(evidence_id)
        self._inventory()  # Reject partial/foreign envelopes; never clean them up.
        key, payload = self._unpack(self._storage._read(path))
        if key != evidence_id:
            raise ValueError
        return StoredObservation(self._decode(payload, book, query_digest), False)

    @_guarded
    def record(self, evidence_id, *, book, query_digest, pages):
        path = self._path(evidence_id)
        payload = encode_observation_pages(book, query_digest=query_digest, pages=pages)
        decoded = self._decode(payload, book, query_digest)
        records = self._inventory()
        if (evidence_id in records and records[evidence_id] != payload
                or evidence_id not in records and len(records) >= MAX_RECORDS):
            raise ValueError
        raw = _canonical(dict(schema='private_observation_evidence_v1', mode=self._mode,
                              scope_digest=self._scope, evidence_id=evidence_id,
                              payload_sha256=sha256(payload).hexdigest(),
                              observation_b64=base64.b64encode(payload).decode('ascii')))
        if len(raw) > MAX_ENVELOPE_BYTES:
            raise ValueError
        created = self._storage._write_once(path, raw, ERROR)
        key, verified = self._unpack(self._storage._read(path))
        if key != evidence_id or verified != payload:
            raise ValueError
        return StoredObservation(decoded, created)
