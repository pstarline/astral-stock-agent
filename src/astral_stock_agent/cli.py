import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from .delivery import DeliveryError, Outbox, Telegram
from .report import InvalidSnapshot, render
from .activity import normalize_activity, render_activity
from .reconciliation import reconcile_daily


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "init-outbox":
        parser = argparse.ArgumentParser(description="Explicitly initialize a private outbox; never sends")
        parser.add_argument("--database", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        try:
            if os.getenv("GITHUB_ACTIONS"):
                raise DeliveryError("operational initialization prohibited in public CI")
            Outbox(args.database, create=True)
            print("Private outbox initialized; persistence and backup still require operator verification")
            return 0
        except (OSError, ValueError, sqlite3.Error, DeliveryError):
            print("Outbox initialization blocked: private storage required")
            return 2
    parser = argparse.ArgumentParser(description="Validate an Astral paper snapshot and preview a daily report")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--send", action="store_true", help="Send to configured private Telegram destination")
    parser.add_argument("--database", type=Path, help="Existing durable PRIVATE runtime volume path")
    parser.add_argument("--activity", action="store_true", help="Preview raw MCP activity; never sends")
    parser.add_argument("--reconcile", action="store_true", help="Preview closing-book reconciliation; never sends")
    parser.add_argument("--us-symbol", action="append", default=[], help="Independently verified US-equity symbol for activity scope")
    args = parser.parse_args(argv)
    try:
        if args.snapshot.stat().st_size > 1_000_000:
            raise InvalidSnapshot("snapshot too large")
        snapshot = json.loads(args.snapshot.read_text())
        now = datetime.now(timezone.utc)
        if args.reconcile:
            if args.send or args.activity:
                raise InvalidSnapshot("reconciliation is preview-only")
            if snapshot["current"]["source"] == "fixture":
                now = datetime.fromisoformat(snapshot["current"]["collected_at"].replace("Z", "+00:00"))
            reconciled = reconcile_daily(snapshot["previous"], snapshot["current"], snapshot["accounting"],
                                         verified_us_symbols=args.us_symbol, now=now)
            print(render(reconciled, now=now)[1])
            return 0
        if args.activity:
            if args.send:
                raise InvalidSnapshot("unreconciled activity delivery prohibited")
            if isinstance(snapshot, dict) and snapshot.get("source") == "fixture":
                now = max(datetime.fromisoformat(x["received_at"].replace("Z", "+00:00"))
                          for x in snapshot["responses"].values())
            observation = normalize_activity(snapshot, verified_us_symbols=args.us_symbol, now=now)
            print(render_activity(observation))
            return 0
        # Fixtures are preview-only, so the checked-in example remains usable.
        if isinstance(snapshot, dict) and snapshot.get("source") == "fixture" and not args.send:
            now = datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
        key, body = render(snapshot, now=now)
        if not args.send:
            print(body)
            return 0
        if snapshot["source"] != "astral_mcp":
            raise InvalidSnapshot("fixture delivery prohibited")
        if os.getenv("GITHUB_ACTIONS"):
            raise DeliveryError("operational delivery prohibited in public CI")
        if args.database is None:
            raise DeliveryError("durable private database required")
        sender = Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
        outbox = Outbox(args.database)
        outbox.enqueue(key, body)
        sent = outbox.deliver(key, sender)
        state = outbox.state(key)
        print("Telegram API accepted report" if sent else f"No new attempt; state={state}")
        return 0 if state == "sent" else 2
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error, DeliveryError):
        # Input and URLs can contain credentials. Fail with a fixed message.
        print("Report blocked: invalid input, configuration, or unconfirmed delivery")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
