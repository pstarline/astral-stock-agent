"""Durable at-most-once attempts. Telegram has no sendMessage idempotency key.

Timeouts and crashes are ambiguous: leave unknown/sending and require operator
reconciliation. This favors avoiding duplicates over guaranteed delivery.
"""

import json
import os
import stat
from pathlib import Path
import re
import sqlite3
from contextlib import contextmanager
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener


class DeliveryError(RuntimeError):
    pass


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Telegram:
    def __init__(self, token, chat_id):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token or ""):
            raise DeliveryError("missing or invalid Telegram token")
        if not re.fullmatch(r"-?[1-9][0-9]*", chat_id or ""):
            raise DeliveryError("private numeric Telegram destination required")
        self._token = token
        self._chat_id = chat_id

    def send(self, message):
        request = Request(
            f"https://api.telegram.org/bot{self._token}/sendMessage",
            data=json.dumps({"chat_id": self._chat_id, "text": message}).encode(),
            headers={"Content-Type": "application/json"}, method="POST",
        )
        # Never include request URL, raw body, or underlying exception in logs.
        try:
            with build_opener(_NoRedirect()).open(request, timeout=15) as response:
                data = response.read(65537)
            if len(data) > 65536:
                raise ValueError
            result = json.loads(data)
            mid = result.get("result", {}).get("message_id")
            chat = result.get("result", {}).get("chat", {}).get("id")
            if result.get("ok") is not True or type(mid) is not int or mid <= 0 or str(chat) != self._chat_id:
                raise ValueError
            return str(mid)
        except HTTPError:
            raise DeliveryError("Telegram delivery unconfirmed; reconcile before retry") from None
        except Exception:
            raise DeliveryError("Telegram delivery unconfirmed; reconcile before retry") from None


class Outbox:
    def __init__(self, path, *, create=False):
        # Creation is an explicit bootstrap action, never an implicit restart.
        if type(create) is not bool:
            raise DeliveryError("explicit outbox initialization flag required")
        # This verifies local POSIX privacy, not volume persistence/backups.
        self.path = str(Path(os.path.abspath(os.fspath(path))))
        self._check_directory()
        if create:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            except OSError:
                raise DeliveryError("private outbox storage unavailable") from None
            else:
                os.close(fd)
        self._identity = self._check_file(Path(self.path))
        with self._connect() as db:
            if create:
                db.execute("""CREATE TABLE IF NOT EXISTS reports (
                    report_key TEXT PRIMARY KEY, body TEXT NOT NULL,
                    state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','unknown')),
                    message_id TEXT)""")
            else:
                # An empty/truncated file is not a fresh operational ledger.
                try:
                    db.execute("SELECT report_key, body, state, message_id FROM reports LIMIT 0")
                except sqlite3.Error:
                    raise DeliveryError("outbox uninitialized; reconcile storage before proceeding") from None

    def _check_directory(self):
        try:
            parent = Path(self.path).parent
            for directory in (parent, *parent.parents):
                info = directory.lstat()
                if not stat.S_ISDIR(info.st_mode) or info.st_uid not in (0, os.geteuid()):
                    raise ValueError
                # Sticky /tmp is supported; other shared writable ancestors
                # could let another UID replace the private directory itself.
                if info.st_mode & 0o022 and not info.st_mode & stat.S_ISVTX:
                    raise ValueError
            info = parent.lstat()
            if info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o777 != 0o700:
                raise ValueError
        except (OSError, ValueError):
            raise DeliveryError("private outbox directory required") from None

    @staticmethod
    def _check_file(path):
        try:
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                    or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
                raise ValueError
            return info.st_dev, info.st_ino
        except (OSError, ValueError):
            raise DeliveryError("private outbox file required") from None

    def _verify_storage(self):
        self._check_directory()
        if self._check_file(Path(self.path)) != self._identity:
            raise DeliveryError("outbox storage changed; reconcile before proceeding")
        # SQLite may create journals or WAL files. Refuse preexisting links or
        # shared-readable sidecars, without modifying their permissions.
        for suffix in ('-journal', '-wal', '-shm'):
            sidecar = Path(self.path + suffix)
            try:
                sidecar.lstat()
            except FileNotFoundError:
                continue
            try:
                self._check_file(sidecar)
            except DeliveryError:
                # A journal may disappear normally between concurrent calls.
                if not sidecar.exists() and not sidecar.is_symlink():
                    continue
                raise

    @contextmanager
    def _connect(self):
        self._verify_storage()
        # mode=rw refuses recreation if storage vanishes after verification.
        db = sqlite3.connect(Path(self.path).as_uri() + "?mode=rw", uri=True, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key, body):
        """Persist an immutable report; identical repeats are idempotent.

        A changed report needs operator reconciliation, even when the previous
        body is still pending. Never replace its body, reset its state, or
        silently treat an old sent report as acceptance of the new content.
        Insert and comparison share one write transaction so competing workers
        cannot accept different bodies for the same key. Errors omit both.
        """
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO reports VALUES (?, ?, 'pending', NULL)", (key, body))
            stored = db.execute("SELECT body FROM reports WHERE report_key=?", (key,)).fetchone()
            if stored is None or stored[0] != body:
                raise DeliveryError("report content conflict; operator reconciliation required")

    def state(self, key):
        with self._connect() as db:
            row = db.execute("SELECT state FROM reports WHERE report_key=?", (key,)).fetchone()
            return row[0] if row else None

    def deliver(self, key, sender):
        with self._connect() as db:
            claimed = db.execute("UPDATE reports SET state='sending' WHERE report_key=? AND state='pending'", (key,)).rowcount
            if not claimed:
                return False
            body = db.execute("SELECT body FROM reports WHERE report_key=?", (key,)).fetchone()[0]
        # Claim commits BEFORE external I/O. A crash never reverts to pending.
        try:
            mid = sender.send(body)
            if not isinstance(mid, str) or not mid:
                raise DeliveryError("delivery unconfirmed")
        except Exception:
            with self._connect() as db:
                db.execute("UPDATE reports SET state='unknown' WHERE report_key=?", (key,))
            raise DeliveryError("delivery unconfirmed; automatic retry blocked") from None
        with self._connect() as db:
            db.execute("UPDATE reports SET state='sent', message_id=? WHERE report_key=?", (mid, key))
        return True
