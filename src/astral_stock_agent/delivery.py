"""Durable at-most-once attempts. Telegram has no sendMessage idempotency key.

Timeouts and crashes are ambiguous: leave unknown/sending and require operator
reconciliation. This favors avoiding duplicates over guaranteed delivery.
"""

import json
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
    def __init__(self, path):
        self.path = str(path)
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS reports (
                report_key TEXT PRIMARY KEY, body TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending','sending','sent','unknown')),
                message_id TEXT)""")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        try:
            db.execute("PRAGMA synchronous=FULL")
            with db:
                yield db
        finally:
            db.close()

    def enqueue(self, key, body):
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO reports VALUES (?, ?, 'pending', NULL)", (key, body))

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
