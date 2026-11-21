"""Explicit first-install storage bootstrap and read-only host inspection.

Never invoked by the worker. Existing or partial stores require reconciliation,
not initialization. This command has no network, strategy or delivery authority.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path

from .delivery import Outbox
from .independent_ledger import IndependentPaperLedger
from .paper_worker import _check_control


class HostStorageError(ValueError):
    pass


DIRECTORIES = ('prices', 'paper', 'outbox', 'control')
ERROR = 'private host storage blocked; inspect storage without reinitializing'


def _root(directory):
    if os.getenv('GITHUB_ACTIONS'):
        raise ValueError
    path = Path(directory)
    if not path.is_absolute():
        raise ValueError
    return IndependentPaperLedger(path)


def initialize(directory):
    """Claim an existing empty 0700 directory once, including across processes.

    Retain the initialization marker on any failure. Do not roll back or repair:
    an operator must inspect a partial disk before deciding what to recover.
    """
    try:
        root = _root(directory)
        if any(root.directory.iterdir()):
            raise ValueError
        # O_EXCL serializes racing initializers; write/fsync precedes subdirs.
        if not root._write_once(root.directory / 'initializing.json', b'{"schema":1}', ERROR):
            raise ValueError
        for name in DIRECTORIES:
            root._check_directory()
            (root.directory / name).mkdir(mode=0o700)
        Outbox(root.directory / 'outbox' / 'reports.sqlite', create=True)
        root._write_once(root.directory / 'initialized.json', b'{"schema":1}', ERROR)
        return dict(status='initialized', worker_enabled=False)
    except Exception:
        raise HostStorageError(ERROR) from None


def inspect(directory):
    """Read-only structure/control/outbox inspection, not full accounting audit."""
    try:
        root = _root(directory)
        if (root._read(root.directory / 'initializing.json') != b'{"schema":1}'
                or root._read(root.directory / 'initialized.json') != b'{"schema":1}'
                or {p.name for p in root.directory.iterdir()}
                != {*DIRECTORIES, 'initializing.json', 'initialized.json'}):
            raise ValueError
        for name in DIRECTORIES:
            IndependentPaperLedger(root.directory / name)
        _check_control(IndependentPaperLedger(root.directory / 'control'))
        counts = Outbox(root.directory / 'outbox' / 'reports.sqlite', read_only=True).status_counts()
        return dict(status='inspected', outbox=counts,
                    delivery_reconciliation_required=any(counts.get(s, 0) for s in ('pending', 'sending', 'unknown')),
                    accounting_verified=False,
                    recipient_receipt_verified=False)
    except Exception:
        raise HostStorageError(ERROR) from None


MAX_FINGERPRINT_FILES = 10_000
MAX_FINGERPRINT_BYTES = 64_000_000


def _fingerprint_scan(root):
    """Bounded, no-link reads; identity observations are not portable digest data."""
    records, observations = {}, {}
    total = 0
    stores = [root] + [IndependentPaperLedger(root.directory / name) for name in DIRECTORIES]
    for store in stores:
        store._check_directory()
        observations[str(store.directory.relative_to(root.directory))] = store._identity
        for path in store.directory.iterdir():
            if store is root and path.name in DIRECTORIES:
                continue
            if len(records) >= MAX_FINGERPRINT_FILES or len(path.name) > 250:
                raise ValueError
            if store.directory == root.directory / 'outbox' and path.name != 'reports.sqlite':
                raise ValueError  # includes hot journals/WAL/SHM: do not checkpoint or repair
            before = path.lstat()
            if total + before.st_size > MAX_FINGERPRINT_BYTES:
                raise ValueError
            data = store._read(path)  # existing owner/0600/single-link/1MB bound
            after = path.lstat()
            identity = lambda v: (v.st_dev, v.st_ino, v.st_size, v.st_mtime_ns, v.st_ctime_ns)
            if identity(before) != identity(after):
                raise ValueError
            relative = str(path.relative_to(root.directory))
            records[relative] = (len(data), hashlib.sha256(data).hexdigest())
            observations[relative] = identity(after)
            total += len(data)
        store._check_directory()
    return records, observations, total


def fingerprint(directory, *, worker_stopped=False):
    """Compare an offline source/restore by portable content fingerprint.

    The caller MUST stop all writers independently. This flag is an attestation,
    not a process lock. Two equal scans detect observed changes, not arbitrary
    concurrent writes/ABA or a crash-consistent live snapshot. No copy/restore,
    database checkpoint, repair, deletion or delivery occurs. Matching hashes
    prove only equality of these bounded files, not accounting or authenticity.
    """
    try:
        if worker_stopped is not True:
            raise ValueError
        root = _root(directory)
        before = _fingerprint_scan(root)
        result = inspect(directory)
        after = _fingerprint_scan(root)
        if before != after:
            raise ValueError
        encoded = json.dumps(before[0], sort_keys=True, separators=(',', ':')).encode()
        return dict(result, status='fingerprinted', schema='paper_storage_fingerprint_v1',
                    content_sha256=hashlib.sha256(encoded).hexdigest(),
                    files=len(before[0]), bytes=before[2],
                    writer_exclusion_verified=False, restore_verified=False)
    except Exception:
        raise HostStorageError(ERROR) from None


def main(argv=None):
    # Avoid argparse reflecting private paths/options into terminal logs.
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise HostStorageError(ERROR)
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('action', choices=('initialize-empty', 'inspect', 'fingerprint'))
        parser.add_argument('--directory', required=True)
        parser.add_argument('--worker-stopped', action='store_true')
        args = parser.parse_args(argv)
        if args.action == 'fingerprint':
            print(json.dumps(fingerprint(args.directory, worker_stopped=args.worker_stopped), sort_keys=True))
            return 0
        if args.worker_stopped:
            raise HostStorageError(ERROR)
        result = (initialize if args.action == 'initialize-empty' else inspect)(args.directory)
        print(json.dumps(result, sort_keys=True))
        return 0
    except HostStorageError:
        print(ERROR)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
