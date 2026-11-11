"""Private append-only checkpoints for independent forward Paper accounting.

The ledger preserves already validated daily calculations.  It does not fetch
prices, generate signals, authorize a strategy, send Telegram messages, or
turn a historical replay into a forward run.  The operator must provide a
private durable POSIX directory and explicitly bootstrap the first session.
"""
import hashlib
import json
import os
import stat
from datetime import date
from decimal import Context, Decimal, DecimalException, localcontext
from pathlib import Path

from .delivery import DeliveryError, validate_telegram_text
from .research_execution import _decimal
from .sessions import latest_closed_session


class IndependentLedgerError(ValueError):
    pass


MAX_BYTES = 1_000_000
SCOPE_FIELDS = ('strategy', 'version', 'symbol', 'label_convention',
                'initial_cash', 'commission_bps', 'slippage_bps')
DECIMAL_FIELDS = (
    'initial_cash', 'cash', 'units', 'equity', 'daily_pnl', 'cumulative_pnl',
    'realized_pnl', 'unrealized_pnl', 'daily_realized_pnl',
    'daily_unrealized_change', 'daily_commission', 'cumulative_commission',
    'commission_bps', 'slippage_bps', 'daily_units_change',
    'pending_close_change', 'max_close_drawdown_fraction',
)


def _canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=True, allow_nan=False,
                      separators=(',', ':')).encode()


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError
        result[key] = value
    return result


def _scope(accounting):
    return {field: accounting[field] for field in SCOPE_FIELDS}


def _checkpoint_hash(accounting, report_body, previous_hash):
    return hashlib.sha256(_canonical({
        'accounting': accounting,
        'report_body': report_body,
        'previous_checkpoint_sha256': previous_hash,
    })).hexdigest()


def _validate_accounting(accounting, *, now):
    try:
        if type(accounting) is not dict or accounting.get('kind') != 'independent_price_only_simulation':
            raise ValueError
        day = date.fromisoformat(accounting['session'])
        if latest_closed_session(now).day != day:
            raise ValueError
        values = {field: _decimal(accounting[field]) for field in DECIMAL_FIELDS}
        if (values['initial_cash'] <= 0 or values['cash'] < 0 or values['units'] < 0
                or values['daily_commission'] < 0 or values['cumulative_commission'] < 0
                or not 0 <= values['commission_bps'] < 10000
                or not 0 <= values['slippage_bps'] < 10000
                or not 0 <= values['max_close_drawdown_fraction'] <= 1):
            raise ValueError
        for field in ('strategy', 'version', 'symbol', 'label_convention', 'price_sha256'):
            value = accounting[field]
            if type(value) is not str or not value:
                raise ValueError
        if (type(accounting['report_sha256']) is not str
                or len(accounting['report_sha256']) != 64
                or any(c not in '0123456789abcdef' for c in accounting['report_sha256'] + accounting['price_sha256'])
                or len(accounting['price_sha256']) != 64):
            raise ValueError
        if (type(accounting['daily_simulated_fills']) is not int
                or type(accounting['cumulative_simulated_fills']) is not int
                or min(accounting['daily_simulated_fills'], accounting['cumulative_simulated_fills']) < 0):
            raise ValueError
        for field in ('provider_authenticity_verified', 'corporate_actions_verified',
                      'signal_causality_verified', 'actual_execution_verified',
                      'deployment_approved'):
            if accounting[field] is not False:
                raise ValueError
        with localcontext(Context(prec=28)):
            if (values['equity'] - values['initial_cash'] != values['cumulative_pnl']
                    or values['realized_pnl'] + values['unrealized_pnl'] != values['cumulative_pnl']
                    or values['daily_realized_pnl'] + values['daily_unrealized_change']
                    != values['daily_pnl']):
                raise ValueError
        return day, values
    except (KeyError, TypeError, ValueError, DecimalException):
        raise IndependentLedgerError('complete independent daily accounting required') from None


class IndependentPaperLedger:
    """Write-once hash-chained daily checkpoints for one execution scope."""

    def __init__(self, directory):
        self.directory = Path(os.path.abspath(os.fspath(directory)))
        self._identity = self._check_directory()

    def _check_directory(self):
        try:
            if os.getenv('GITHUB_ACTIONS'):
                raise ValueError
            for path in (self.directory, *self.directory.parents):
                info = path.lstat()
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
            raise IndependentLedgerError('private Paper ledger directory unavailable') from None

    def _path(self, name):
        self._check_directory()
        return self.directory / (hashlib.sha256(name.encode()).hexdigest() + '.json')

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
            raise IndependentLedgerError('Paper ledger checkpoint missing or storage unverified') from None
        finally:
            if fd is not None:
                os.close(fd)

    def _sync(self, path):
        self._check_directory()
        for target, flags in ((path, os.O_RDONLY | os.O_NOFOLLOW),
                              (self.directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)):
            fd = os.open(target, flags)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _write_once(self, path, data, conflict):
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        except FileExistsError:
            if self._read(path) != data:
                raise IndependentLedgerError(conflict)
            self._sync(path)
            return False
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
        self._sync(path)
        if self._read(path) != data:
            raise IndependentLedgerError('Paper ledger write could not be verified')
        return True

    def _load_checkpoint(self, day):
        try:
            envelope = json.loads(self._read(self._path('session:' + day)), object_pairs_hook=_unique)
            accounting = envelope['accounting']
            report_body = envelope['report_body']
            previous_hash = envelope['previous_checkpoint_sha256']
            expected = _checkpoint_hash(accounting, report_body, previous_hash)
            if envelope != {'version': 1, 'accounting': accounting, 'report_body': report_body,
                            'previous_checkpoint_sha256': previous_hash,
                            'checkpoint_sha256': expected} or accounting['session'] != day:
                raise ValueError
            validate_telegram_text(report_body)
            if accounting['report_sha256'] != hashlib.sha256(report_body.encode()).hexdigest():
                raise ValueError
            return accounting, report_body, expected
        except (KeyError, TypeError, ValueError, json.JSONDecodeError, DeliveryError):
            raise IndependentLedgerError('Paper ledger checkpoint invalid; reconciliation required') from None

    def append(self, accounting, report_body, *, now, bootstrap=False):
        """Persist the latest closed session after restart/transition checks.

        ``bootstrap=True`` is only valid for the scope's immutable first session.
        A normal append requires the exact previous NYSE session and verifies
        cash/equity/P&L, fill counts, fees, holdings and the pending close signal.
        No report is queued or sent here.
        """
        try:
            if type(bootstrap) is not bool:
                raise ValueError
            day, current = _validate_accounting(accounting, now=now)
            validate_telegram_text(report_body)
            if accounting['report_sha256'] != hashlib.sha256(report_body.encode()).hexdigest():
                raise ValueError
            scope = _scope(accounting)
            scope_data = _canonical({'version': 1, 'scope': scope,
                                     'first_session': day.isoformat()})
            scope_path = self._path('scope')
            if bootstrap:
                self._write_once(scope_path, scope_data,
                                 'Paper ledger bootstrap scope conflict; reconciliation required')
                previous_hash = None
            else:
                if self._read(scope_path) != scope_data:
                    # The immutable first session intentionally differs after bootstrap;
                    # compare the stored scope separately below.
                    stored_scope = json.loads(self._read(scope_path), object_pairs_hook=_unique)
                    if (stored_scope.get('version') != 1 or stored_scope.get('scope') != scope
                            or stored_scope.get('first_session') == day.isoformat()):
                        raise ValueError
                previous_session = latest_closed_session(
                    latest_closed_session(now).opens_at).day.isoformat()
                previous_accounting, _, previous_hash = self._load_checkpoint(previous_session)
                if _scope(previous_accounting) != scope:
                    raise ValueError
                previous = {field: _decimal(previous_accounting[field]) for field in DECIMAL_FIELDS}
                with localcontext(Context(prec=28)):
                    if (current['daily_pnl'] != current['equity'] - previous['equity']
                            or accounting['cumulative_simulated_fills']
                            != previous_accounting['cumulative_simulated_fills']
                            + accounting['daily_simulated_fills']
                            or current['cumulative_commission']
                            != previous['cumulative_commission'] + current['daily_commission']
                            or current['units'] != previous['units'] + current['daily_units_change']
                            or current['daily_units_change'] != previous['pending_close_change']):
                        raise ValueError
            checkpoint_hash = _checkpoint_hash(accounting, report_body, previous_hash)
            envelope = {'version': 1, 'accounting': accounting, 'report_body': report_body,
                        'previous_checkpoint_sha256': previous_hash,
                        'checkpoint_sha256': checkpoint_hash}
            created = self._write_once(self._path('session:' + day.isoformat()),
                                       _canonical(envelope),
                                       'Paper ledger checkpoint conflict; reconciliation required')
            return {'created': created, 'session': day.isoformat(),
                    'checkpoint_sha256': checkpoint_hash,
                    'bootstrap': bootstrap}
        except IndependentLedgerError:
            raise
        except (OSError, KeyError, TypeError, ValueError, DecimalException,
                json.JSONDecodeError, DeliveryError):
            raise IndependentLedgerError('Paper ledger append blocked; reconciliation required') from None
