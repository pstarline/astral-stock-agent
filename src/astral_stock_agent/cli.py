import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from datetime import datetime, timezone

from .delivery import DeliveryError, Outbox, Telegram, validate_telegram_text
from .report import InvalidSnapshot, render
from .activity import normalize_activity, render_activity
from .reconciliation import reconcile_daily
from .preflight import normalize_preflight, render_preflight
from .account_observation import render_account_observation
from .closing_books import ClosingBooks
from .account_status import render_account_status


def _unique_json_object(pairs):
    """Reject ambiguous fields at every JSON object, including escaped names.

    Never include the key or either value in the error: inputs are private.
    Separate objects may use the same field names normally.
    """
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidSnapshot("ambiguous JSON object")
        result[key] = value
    return result


class _InvalidArguments(ValueError):
    pass


class _PrivateArgumentParser(argparse.ArgumentParser):
    def error(self, message):
        # argparse diagnostics include unknown values, flags and private paths.
        # Never emit that diagnostic, even for missing arguments.
        raise _InvalidArguments("invalid command arguments") from None


def main(argv=None):
    try:
        return _main(argv)
    except _InvalidArguments:
        print("Command blocked: invalid arguments; use --help", file=sys.stderr)
        return 2


def _main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "preview-market-sma":
        parser = _PrivateArgumentParser(description="Collect official compact daily data and preview a research candidate; never records or sends")
        parser.add_argument("configuration", type=Path)
        parser.add_argument("--us-symbol", action="append", required=True)
        args = parser.parse_args(argv[1:])
        try:
            if os.getenv("GITHUB_ACTIONS"):
                raise ValueError("private market collection prohibited in public CI")
            from .market_data import collect_sma_preview
            with args.configuration.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("configuration too large")
            config = json.loads(raw, object_pairs_hook=_unique_json_object)
            _, _, _, body = collect_sma_preview(config,
                api_key=os.getenv("ALPHAVANTAGE_API_KEY"), verified_us_symbols=args.us_symbol)
            print(body)
            return 0
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, DeliveryError):
            print("Market preview blocked: private configuration/key or current daily data unavailable")
            return 2
    if argv and argv[0] == "record-simulation":
        parser = _PrivateArgumentParser(
            description="Record one independently calculated closed Paper session; never sends")
        parser.add_argument("manifest", type=Path)
        parser.add_argument("prices", type=Path)
        parser.add_argument("--ledger-directory", type=Path, required=True)
        parser.add_argument("--us-symbol", action="append", required=True)
        parser.add_argument("--bootstrap", action="store_true")
        args = parser.parse_args(argv[1:])
        try:
            if os.getenv("GITHUB_ACTIONS"):
                raise ValueError("private simulation ledger prohibited in public CI")
            from .research_data import MAX_BYTES
            from .independent_paper import preview_independent_paper
            from .independent_ledger import IndependentPaperLedger
            with args.manifest.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("manifest too large")
            manifest = json.loads(raw, object_pairs_hook=_unique_json_object)
            with args.prices.open("rb") as stream:
                prices = stream.read(MAX_BYTES + 1)
            now = datetime.now(timezone.utc)
            accounting, body = preview_independent_paper(
                manifest, prices, verified_us_symbols=args.us_symbol, now=now)
            result = IndependentPaperLedger(args.ledger_directory).append(
                accounting, body, now=now, bootstrap=args.bootstrap)
            print(json.dumps({key: result[key] for key in ('bootstrap', 'created', 'session')},
                             sort_keys=True))
            return 0
        except (OSError, ValueError, TypeError, KeyError, AttributeError,
                OverflowError, DeliveryError):
            print("Simulation record blocked: private ledger or complete closed-session evidence required")
            return 2
    if argv and argv[0] in ("preview-simulation", "preview-sma"):
        parser = _PrivateArgumentParser(
            description="Preview independent price-only signals/accounting; never sends")
        parser.add_argument("manifest", type=Path)
        parser.add_argument("prices", type=Path)
        parser.add_argument("--us-symbol", action="append", required=True)
        args = parser.parse_args(argv[1:])
        try:
            # Private prices/signals must not enter public CI output.
            if os.getenv("GITHUB_ACTIONS"):
                raise ValueError("private simulation prohibited in public CI")
            from .research_data import MAX_BYTES
            from .independent_paper import preview_independent_paper
            with args.manifest.open("rb") as stream:
                raw = stream.read(1_000_001)
            if len(raw) > 1_000_000:
                raise ValueError("manifest too large")
            manifest = json.loads(raw, object_pairs_hook=_unique_json_object)
            with args.prices.open("rb") as stream:
                prices = stream.read(MAX_BYTES + 1)
            if argv[0] == "preview-sma":
                from .independent_signals import preview_independent_sma
                _, _, body = preview_independent_sma(manifest, prices,
                    verified_us_symbols=args.us_symbol, now=datetime.now(timezone.utc))
            else:
                _, body = preview_independent_paper(manifest, prices,
                    verified_us_symbols=args.us_symbol, now=datetime.now(timezone.utc))
            print(body)
            return 0
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError, DeliveryError):
            print("Simulation preview blocked: complete private prices, signals and explicit scope required")
            return 2
    if argv and argv[0] == "audit-research":
        # This command handles private recorded evidence only, never API calls.
        # Suppress argparse's reflection of mistakenly pasted configuration.
        class ResearchParser(argparse.ArgumentParser):
            def error(self, message):
                raise ValueError("invalid research arguments")

        parser = ResearchParser(description="Compare private recorded SMA evidence; never deploys or sends")
        parser.add_argument("manifest", type=Path)
        parser.add_argument("prices", type=Path)
        parser.add_argument("--us-symbol", action="append", required=True)
        try:
            args = parser.parse_args(argv[1:])
            if os.getenv("GITHUB_ACTIONS"):
                raise ValueError("private research prohibited in public CI")
            from .research_data import MAX_BYTES
            from .research_review import review_sma_evidence
            # Bound actual reads as well as reported file size.
            with args.manifest.open("rb") as stream:
                raw_manifest = stream.read(1_000_001)
            if len(raw_manifest) > 1_000_000:
                raise ValueError("manifest too large")
            manifest = json.loads(raw_manifest, object_pairs_hook=_unique_json_object)
            with args.prices.open("rb") as stream:
                prices = stream.read(MAX_BYTES + 1)
            result = review_sma_evidence(manifest, prices, verified_us_symbols=args.us_symbol,
                                         now=datetime.now(timezone.utc))
            print(json.dumps(result, sort_keys=True))
            return 0 if result["curves_match"] else 2
        except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError):
            print("Research audit blocked: complete private evidence and explicit scope required")
            return 2
    if argv and argv[0] == "check-telegram":
        parser = _PrivateArgumentParser(description="Check private bot/chat metadata; never sends or reads messages")
        # argparse's default unknown-argument error echoes supplied values.
        # Reject them without reflecting a mistakenly pasted secret.
        if argv[1:] not in ([], ["-h"], ["--help"]):
            print("Telegram check blocked: no commandline configuration accepted")
            return 2
        parser.parse_args(argv[1:])
        try:
            if os.getenv("GITHUB_ACTIONS"):
                raise DeliveryError("operational connection check prohibited in public CI")
            sender = Telegram(os.getenv("TELEGRAM_BOT_TOKEN"), os.getenv("TELEGRAM_CHAT_ID"))
            print(json.dumps(sender.check_connection(), sort_keys=True))
            return 0
        except (OSError, ValueError, TypeError, DeliveryError):
            print("Telegram check blocked: private configuration or connection unverified")
            return 2
    if argv and argv[0] == "inspect-outbox":
        parser = _PrivateArgumentParser(description="Read private outbox state counts; never sends or repairs")
        parser.add_argument("--database", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        try:
            if os.getenv("GITHUB_ACTIONS"):
                raise DeliveryError("operational inspection prohibited in public CI")
            counts = Outbox(args.database, read_only=True).status_counts()
            # A pending row may be left by a crash after enqueue. The status
            # runner skips it on restart, so inspection must require attention
            # too. This does not classify it as sent or authorize a retry.
            unresolved = counts["pending"] + counts["sending"] + counts["unknown"]
            print(json.dumps({"counts": counts, "operator_reconciliation_required": bool(unresolved)}, sort_keys=True))
            return 2 if unresolved else 0
        except (OSError, ValueError, sqlite3.Error, DeliveryError):
            print("Outbox inspection blocked: private initialized storage required")
            return 2
    if argv and argv[0] == "init-outbox":
        parser = _PrivateArgumentParser(description="Explicitly initialize a private outbox; never sends")
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
    parser = _PrivateArgumentParser(description="Validate an Astral paper snapshot and preview a daily report")
    parser.add_argument("snapshot", type=Path)
    parser.add_argument("--send", action="store_true", help="Send to configured private Telegram destination")
    parser.add_argument("--database", type=Path, help="Existing durable PRIVATE runtime volume path")
    parser.add_argument("--preflight", action="store_true", help="Preview account/deployment prerequisites; never sends")
    parser.add_argument("--account-observation", action="store_true", help="Preview official cash fields with unknown accounting; never sends")
    parser.add_argument("--account-status", action="store_true", help="Limited account/deployment alert; unknown accounting stays unknown")
    parser.add_argument("--activity", action="store_true", help="Preview raw MCP activity; never sends")
    parser.add_argument("--reconcile", action="store_true", help="Reconcile closing books; required for operational --send")
    parser.add_argument("--book-directory", type=Path,
                        help="Load previous and persist current private closing book; requires --reconcile")
    parser.add_argument("--us-symbol", action="append", default=[], help="Independently verified US-equity symbol for activity scope")
    args = parser.parse_args(argv)
    try:
        if args.account_status and (args.reconcile or args.activity or args.preflight
                                    or args.account_observation or args.book_directory is not None):
            raise InvalidSnapshot("limited account status is a separate report mode")
        if args.book_directory is not None:
            if not args.reconcile or args.preflight or args.account_observation or args.activity:
                raise InvalidSnapshot("closing-book storage requires separate reconciliation mode")
            if os.getenv("GITHUB_ACTIONS"):
                raise InvalidSnapshot("private closing-book access prohibited in public CI")
        if args.snapshot.stat().st_size > 1_000_000:
            raise InvalidSnapshot("snapshot too large")
        snapshot = json.loads(args.snapshot.read_text(), object_pairs_hook=_unique_json_object)
        now = datetime.now(timezone.utc)
        if args.send:
            if not args.reconcile and not args.account_status:
                raise InvalidSnapshot("daily accounting reconciliation required for delivery")
            if os.getenv("GITHUB_ACTIONS"):
                raise DeliveryError("operational delivery prohibited in public CI")
        if args.account_observation:
            if args.send or args.preflight or args.activity or args.reconcile:
                raise InvalidSnapshot("account observation is a separate preview-only operation")
            if isinstance(snapshot, dict) and snapshot.get("source") == "fixture":
                now = max(datetime.fromisoformat(x["received_at"].replace("Z", "+00:00"))
                          for x in snapshot["responses"].values())
            if os.getenv("GITHUB_ACTIONS") and snapshot.get("source") != "fixture":
                raise InvalidSnapshot("private account preview prohibited in public CI")
            print(render_account_observation(snapshot, verified_us_symbols=args.us_symbol, now=now))
            return 0
        if args.preflight:
            if args.send or args.activity or args.reconcile:
                raise InvalidSnapshot("preflight is a separate preview-only operation")
            if isinstance(snapshot, dict) and snapshot.get("source") == "fixture":
                now = max(datetime.fromisoformat(x["received_at"].replace("Z", "+00:00"))
                          for x in snapshot["responses"].values())
            print(render_preflight(normalize_preflight(snapshot, verified_us_symbols=args.us_symbol, now=now)))
            return 0
        if args.reconcile:
            if args.activity:
                raise InvalidSnapshot("reconciliation cannot be mixed with activity preview")
            if snapshot["current"]["source"] == "fixture" and not args.send:
                now = datetime.fromisoformat(snapshot["current"]["collected_at"].replace("Z", "+00:00"))
            if args.book_directory is not None:
                if "previous" in snapshot:
                    raise InvalidSnapshot("inline and stored baselines cannot be mixed")
                if args.send and snapshot["current"]["source"] != "astral_mcp":
                    raise InvalidSnapshot("fixture delivery prohibited")
                reconciled = ClosingBooks(args.book_directory).reconcile_and_save(
                    snapshot["current"], snapshot["accounting"],
                    verified_us_symbols=args.us_symbol, now=now)
            else:
                reconciled = reconcile_daily(snapshot["previous"], snapshot["current"], snapshot["accounting"],
                                             verified_us_symbols=args.us_symbol, now=now)
            if not args.send:
                print(render(reconciled, now=now)[1])
                return 0
            snapshot = reconciled
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
        if isinstance(snapshot, dict) and snapshot.get("source") == "fixture" and not args.send and not args.account_status:
            now = datetime.fromisoformat(snapshot["collected_at"].replace("Z", "+00:00"))
        if args.account_status:
            if isinstance(snapshot, dict) and snapshot.get("source") == "fixture" and not args.send:
                now = max(datetime.fromisoformat(x["received_at"].replace("Z", "+00:00"))
                          for x in snapshot["responses"].values())
            if os.getenv("GITHUB_ACTIONS") and snapshot.get("source") != "fixture":
                raise InvalidSnapshot("private account status prohibited in public CI")
            key, body = render_account_status(snapshot, verified_us_symbols=args.us_symbol, now=now)
        else:
            key, body = render(snapshot, now=now)
        if not args.send:
            print(body)
            return 0
        if snapshot["source"] != "astral_mcp":
            raise InvalidSnapshot("fixture delivery prohibited")
        validate_telegram_text(body)
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

