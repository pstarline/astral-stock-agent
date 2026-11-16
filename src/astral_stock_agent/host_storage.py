"""Explicit first-install storage bootstrap and read-only host inspection.

Never invoked by the worker. Existing or partial stores require reconciliation,
not initialization. This command has no network, strategy or delivery authority.
"""
import argparse
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


def main(argv=None):
    # Avoid argparse reflecting private paths/options into terminal logs.
    class Parser(argparse.ArgumentParser):
        def error(self, message):
            raise HostStorageError(ERROR)
    try:
        parser = Parser(description=__doc__)
        parser.add_argument('action', choices=('initialize-empty', 'inspect'))
        parser.add_argument('--directory', required=True)
        args = parser.parse_args(argv)
        result = (initialize if args.action == 'initialize-empty' else inspect)(args.directory)
        print(json.dumps(result, sort_keys=True))
        return 0
    except HostStorageError:
        print(ERROR)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
