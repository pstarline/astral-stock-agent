"""Private, immutable reporting checkpoints; never an authoritative trade ledger.

The operator supplies durable storage and trusted normalized closing books.
Local permissions/fsync do not prove persistence, backup or source authenticity.
No network, credentials, orders or Telegram access is available here.
"""
import hashlib
import json
import os
import stat
from datetime import date
from pathlib import Path

from .reconciliation import SCOPE, reconcile_daily
from .report import render, text, timestamp
from .sessions import latest_closed_session, require_closed_session


class ClosingBookError(ValueError):
    pass


MAX_BYTES = 1_000_000


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ClosingBookError('ambiguous private closing book')
        result[key] = value
    return result


def _validate(book, now):
    render(book, now=now)
    # General previews allow near-close marks; checkpoints require exact close.
    if timestamp(book['valuation_at']) != timestamp(book['session_closed_at']):
        raise ClosingBookError('exact closing book required')
    return (text(book['source']), *(text(book[k]) for k in SCOPE))


class ClosingBooks:
    def __init__(self, directory):
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self._identity = self._check_directory()

    def _check_directory(self):
        try:
            if os.getenv('GITHUB_ACTIONS'):
                raise ValueError
            for p in (self.directory, *self.directory.parents):
                info = p.lstat()
                if (not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid())
                        or (info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX)):
                    raise ValueError
            info = self.directory.lstat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) != 0o700:
                raise ValueError
            identity = info.st_dev, info.st_ino
            if hasattr(self, '_identity') and identity != self._identity:
                raise ValueError
            return identity
        except (OSError, ValueError):
            raise ClosingBookError('private closing-book directory unavailable') from None

    def _path(self, scope, day):
        self._check_directory()
        key = hashlib.sha256(json.dumps([*scope, day], ensure_ascii=True).encode()).hexdigest()
        return self.directory / (key + '.json')

    def _read(self, path):
        fd = None
        try:
            self._check_directory()
            before = path.lstat()
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1
                    or (info.st_dev, info.st_ino) != (before.st_dev, before.st_ino)
                    or not 0 < info.st_size <= MAX_BYTES):
                raise ValueError
            with os.fdopen(fd, 'rb') as stream:
                fd = None
                data = stream.read(MAX_BYTES + 1)
            after = path.lstat()
            if len(data) != info.st_size or (after.st_dev, after.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError
            self._check_directory()
            return data
        except (OSError, ValueError):
            raise ClosingBookError('closing book missing or storage unverified') from None
        finally:
            if fd is not None:
                os.close(fd)

    def save(self, book, *, now):
        """Write once; identical repeats are allowed, changed books are blocked.

        An interrupted write remains blocked for operator reconciliation. Never
        truncate, overwrite, delete or silently recreate an existing checkpoint.
        """
        try:
            scope = _validate(book, now)
            day = date.fromisoformat(book['trading_day']).isoformat()
            data = _canonical({'version': 1, 'book': book,
                               'sha256': hashlib.sha256(_canonical(book)).hexdigest()})
            if len(data) > MAX_BYTES:
                raise ValueError
            path = self._path(scope, day)
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                if self._read(path) != data:
                    raise ClosingBookError('closing book conflict; reconciliation required')
                self._sync(path)
                return False
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
                stream.flush()
            self._sync(path)
            if self._read(path) != data:
                raise ValueError
            return True
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ClosingBookError('closing book not saved; storage or evidence unverified') from None

    def _sync(self, path):
        # Repeat both syncs on an identical retry after an uncertain write.
        self._check_directory()
        for target, flags in ((path, os.O_RDONLY | os.O_NOFOLLOW),
                              (self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)):
            fd = os.open(target, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def previous_for(self, current, *, now):
        """Require the exact previous NYSE session and unchanged execution scope."""
        try:
            scope = _validate(current, now)
            session = require_closed_session(date.fromisoformat(current['trading_day']), now)
            previous_day = latest_closed_session(session.opens_at).day.isoformat()
            raw = self._read(self._path(scope, previous_day))
            envelope = json.loads(raw, object_pairs_hook=_unique)
            previous = envelope['book']
            if (type(envelope['version']) is not int or envelope['version'] != 1
                    or envelope['sha256'] != hashlib.sha256(_canonical(previous)).hexdigest()):
                raise ValueError
            if (_validate(previous, timestamp(previous['collected_at'])) != scope
                    or previous['trading_day'] != previous_day):
                raise ValueError
            return previous
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            raise ClosingBookError('matching previous closing book unavailable') from None

    def reconcile_and_save(self, current, accounting, *, verified_us_symbols, now):
        """Reconcile first, then preserve the unchanged current source book.

        Delivery is a separate step: Telegram failure must not remove a valid
        checkpoint. A missing/changed baseline never falls back to zero cash.
        """
        previous = self.previous_for(current, now=now)
        result = reconcile_daily(previous, current, accounting,
                                 verified_us_symbols=verified_us_symbols, now=now)
        self.save(current, now=now)
        return result
