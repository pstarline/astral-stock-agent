"""Opt-in, private Explore research intake; no strategy adoption or execution.

The orchestrator supplies an authorized async official MCP callback. This module
has no HTTP client, credentials, source execution, copy/save, order or deployment
interface. Missing evidence stays None. Observed payload hashes are change
evidence, NEVER provider strategy versions or independent validation.
"""
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Context, Decimal
import hashlib
import json
import math
import os
import re
import unicodedata

from .independent_ledger import IndependentPaperLedger, _canonical, _unique


INTERVAL = timedelta(days=7)
TIMEOUT_SECONDS = 60
MAX_HISTORY = 520
ERROR = 'private public-strategy discovery blocked; reconcile evidence and journal'
MATERIAL_FIELDS = ('assets', 'timeframe', 'fill_price_mode', 'definition_sha256',
    'backtest_period', 'reported_backtest', 'commission_and_slippage_bps',
    'initial_capital', 'data_source', 'data_provider')


class DiscoveryError(ValueError):
    pass


@dataclass(frozen=True)
class DiscoveryBatch:
    status: str
    considered: int = 0
    skipped: int = 0
    unchanged: int = 0
    change_unconfirmed: int = 0
    more_available: bool = False
    candidates: tuple = field(default=(), repr=False)
    weekly_automation_active: bool = False
    deployment_approved: bool = False


def _time(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError
    return value.astimezone(timezone.utc)


def _stamp(value):
    if type(value) is not str:
        raise ValueError
    return _time(datetime.fromisoformat(value.replace('Z', '+00:00')))


def _text(value, limit=200):
    if value is None:
        return None
    if (type(value) is not str or not value.strip() or len(value) > limit
            or any(unicodedata.category(c).startswith('C') for c in value)):
        raise ValueError
    return value


def _number(value, *, count=False):
    if value is None:
        return None
    if count:
        if type(value) is not int or value < 0:
            raise ValueError
        return value
    if type(value) not in (int, float, str) or (type(value) is float and not math.isfinite(value)):
        raise ValueError
    if len(str(value)) > 100:
        raise ValueError
    result = Decimal(str(value))
    if not result.is_finite() or (result and abs(result.adjusted()) > 100):
        raise ValueError
    return str(result.normalize(Context(prec=100)))


def _digest(value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(raw) > 2_000_000:
        raise ValueError
    return hashlib.sha256(raw).hexdigest()


def _body(response, kind):
    if type(response) is not dict or response.get('isError'):
        raise ValueError
    body = response.get('structuredContent', response)
    if (type(body) is not dict or body.get('type') != kind
            or body.get('status') == 'needs_correction'
            or any(body.get(k, False) is not False for k in ('truncated', 'response_truncated'))):
        raise ValueError
    return body


def _identity(item):
    if (type(item) is not dict or item.get('item_type') != 'strategy'
            or type(item.get('published_id')) is not int or item['published_id'] <= 0
            or type(item.get('slug')) is not str
            or not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', item['slug'])
            or len(item['slug']) > 200):
        raise ValueError
    # Only public publication references; saved/working/user/account IDs are not copied.
    urls = tuple('https://' + host + '/explore/strategy/' + item['slug']
                 for host in ('heyastral.ai', 'classic.heyastral.ai'))
    if item.get('app_url') not in urls:
        raise ValueError  # no signed URLs, redirects, queries or arbitrary source hosts
    return item['published_id'], item['slug']


def _assets(item):
    assets = item['strategy'].get('assets')
    if (type(assets) is not list or not 1 <= len(assets) <= 100
            or any(type(s) is not str or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', s) for s in assets)
            or len(set(assets)) != len(assets)):
        raise ValueError
    return sorted(assets)


def _metrics(raw):
    if raw is None:
        raw = {}
    if type(raw) is not dict:
        raise ValueError
    stats = raw.get('trade_statistics')
    if stats is None:
        stats = {}
    if type(stats) is not dict:
        raise ValueError
    return dict(total_return_fraction=_number(raw.get('total_return')),
        max_drawdown_signed_fraction=_number(raw.get('max_drawdown')),
        sharpe=_number(raw.get('sharpe_ratio')), num_trades=_number(raw.get('num_trades'), count=True),
        trade_statistics={k: _number(stats.get(k), count=True)
            for k in ('completed_positions', 'closed_trades', 'executed_orders', 'open_positions')},
        trade_count_definition=_text(stats.get('statistics_basis'), 80))


def _definition_scope(payload, assets, timeframe):
    """Cross-check the observed v2 universe/dataset shape, without running it.

    IDs are references, not necessarily ticker symbols. Dataset names do not
    establish bar sizes. Missing declarations stay unconfirmed; contradictory
    symbol scopes or explicit bar sizes block the batch instead of choosing one.
    Provider asset-class labels are not proof of an independently verified market.
    """
    flags = []
    definition = payload.get('strategy') if payload is not None else None
    if definition is None:
        definition = {}
    if type(definition) is not dict:
        raise ValueError
    universe = definition.get('universe')
    references = None
    if universe is not None:
        if type(universe) is not dict:
            raise ValueError
        declarations = universe.get('assets')
        if declarations is not None:
            if type(declarations) is not list or not 1 <= len(declarations) <= 100:
                raise ValueError
            references = {}
            for asset in declarations:
                if type(asset) is not dict:
                    raise ValueError
                reference, symbol = asset.get('id'), asset.get('symbol')
                if (type(reference) is not str or not reference or len(reference) > 80
                        or _text(reference, 80) is None or reference in references
                        or type(symbol) is not str
                        or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', symbol)):
                    raise ValueError
                references[reference] = symbol
                asset_type, currency = _text(asset.get('type'), 40), _text(asset.get('currency'), 20)
                if asset_type is None:
                    flags.append('definition_asset_class_unconfirmed')
                elif asset_type != 'equity':
                    flags.append('definition_asset_class_requires_review')
                if currency is None:
                    flags.append('definition_currency_unconfirmed')
                elif currency != 'USD':
                    flags.append('definition_currency_requires_review')
            if set(references.values()) != set(assets):
                raise ValueError
    if references is None:
        flags.append('definition_universe_unconfirmed')
    datasets = definition.get('datasets')
    if datasets is None:
        flags.append('definition_datasets_unconfirmed')
    else:
        if type(datasets) is not list or not 1 <= len(datasets) <= 100:
            raise ValueError
        for dataset in datasets:
            if type(dataset) is not dict:
                raise ValueError
            scope = dataset.get('assets')
            if scope is None or references is None:
                flags.append('dataset_asset_scope_unconfirmed')
            elif (type(scope) is not list or not 1 <= len(scope) <= 100
                    or any(type(ref) is not str or ref not in references for ref in scope)
                    or len(set(scope)) != len(scope)):
                raise ValueError
            kind = _text(dataset.get('kind'), 40)
            if kind != 'ohlcv':
                flags.append('dataset_kind_requires_review')
            else:
                bar_size = _text(dataset.get('timeframe'), 20)
                if bar_size is None or timeframe is None:
                    flags.append('dataset_timeframe_unconfirmed')
                elif bar_size != timeframe:
                    raise ValueError
    return sorted(set(flags))


def _project(summary, detail, observed_at):
    public_id, slug = _identity(summary)
    if _identity(detail) != (public_id, slug) or _assets(summary) != _assets(detail):
        raise ValueError
    strategy = detail['strategy']
    if strategy.get('visibility') != 'public':
        raise ValueError
    payload = strategy.get('payload')
    period, costs, capital, timeframe, rules_hash = None, None, None, None, None
    if payload is not None:
        if (type(payload) is not dict or payload.get('t') != 'run_strategy'
                or type(payload.get('v')) is not int or payload['v'] != 2
                or payload.get('mode') != 'backtest'):
            raise ValueError
        # Hash, never store the definition. v=2 is a format version, not a strategy version.
        rules_hash = _digest(payload)
        engine = payload.get('engine')
        if engine is None:
            engine = {}
        if type(engine) is not dict:
            raise ValueError
        timeframe = _text(engine.get('bar_size'), 20)
        timing = _text(engine.get('fill_price_mode'), 40)
        start, end = payload.get('start'), payload.get('end')
        if start is not None or end is not None:
            if _stamp(start) >= _stamp(end) or _stamp(end) > observed_at:
                raise ValueError
            period = dict(start=_stamp(start).isoformat(), end=_stamp(end).isoformat())
        raw_costs = payload.get('costs')
        if raw_costs is not None:
            if type(raw_costs) is not dict:
                raise ValueError
            costs = {k: _number(raw_costs.get(k)) for k in ('commission_bps', 'slippage_bps')}
            if any(v is not None and not 0 <= Decimal(v) < 10000 for v in costs.values()):
                raise ValueError
        capital = _number(payload.get('capital'))
        if capital is not None and Decimal(capital) <= 0:
            raise ValueError
    else:
        timing = None
    scope_flags = _definition_scope(payload, _assets(detail), timeframe)
    raw_detail = strategy.get('performance_metrics')
    if raw_detail is not None and type(raw_detail) is not dict:
        raise ValueError
    # Search and detail evidence are separate; never silently merge missing/conflicting metrics.
    reported = dict(search=_metrics(summary['strategy'].get('performance_metrics')),
        detail=_metrics((raw_detail or {}).get('performance_metrics')),
        detail_wrapper=_metrics(raw_detail))
    author = detail.get('author')
    if author is None:
        author = {}
    if type(author) is not dict:
        raise ValueError
    search_metrics = summary['strategy'].get('performance_metrics')
    if search_metrics is None:
        search_metrics = {}
    disclosure = search_metrics.get('market_data_disclosure')
    if disclosure is None:
        disclosure = {}
    if type(disclosure) is not dict:
        raise ValueError
    published = detail.get('published_at')
    if published is not None:
        published = _stamp(published)
        if published > observed_at:
            raise ValueError
    candidate = dict(schema='public_strategy_review_candidate_v1', origin='public',
        source=dict(provider='astral_explore', published_id=public_id, slug=slug,
            url=detail['app_url'], author_display_name=_text(author.get('display_name')),
            author_handle=_text(author.get('handle')), provider_version=None,
            published_at=published.isoformat() if published else None),
        title=_text(detail.get('title')), observed_at=observed_at.isoformat(),
        assets=_assets(detail), timeframe=timeframe, fill_price_mode=timing,
        definition_sha256=rules_hash, backtest_period=period, reported_backtest=reported,
        commission_and_slippage_bps=costs, initial_capital=capital,
        data_source=_text(disclosure.get('source')), data_provider=_text(disclosure.get('provider')),
        entry_rules_summary=None, exit_rules_summary=None, sizing_summary=None,
        paper_performance=None, validation_status='needs_independent_review',
        required_checks=['rules', 'period', 'costs', 'execution_semantics', 'data_quality',
                         'risk', 'overfitting', 'reproducibility', 'forward_paper'],
        overfitting_reviewed=False, reproducibility_verified=False, deployment_approved=False)
    flags = ['provider_version_unconfirmed', 'entry_exit_and_sizing_review_required',
             'cost_sensitivity_unverified', 'paper_performance_unconfirmed',
             'overfitting_and_reproducibility_review_required']
    flags.extend(scope_flags)
    if rules_hash is None:
        flags.append('definition_unavailable')
    if period is None:
        flags.append('backtest_period_unconfirmed')
    elif _stamp(period['end']) - _stamp(period['start']) < timedelta(days=180):
        flags.append('sample_under_180_days')
    if costs is None or any(v is None for v in costs.values()):
        flags.append('costs_unconfirmed')
    if candidate['data_provider'] is None:
        flags.append('data_provider_unconfirmed')
    for origin, metrics in reported.items():
        ret, sharpe = metrics['total_return_fraction'], metrics['sharpe']
        count = metrics['trade_statistics']['completed_positions']
        if ret is not None and Decimal(ret).copy_abs() > 1:
            flags.append(origin + '_reported_abs_return_above_100pct')
        if sharpe is not None and Decimal(sharpe).copy_abs() > 3:
            flags.append(origin + '_reported_abs_sharpe_above_3')
        if count is not None and count < 30:
            flags.append(origin + '_completed_positions_below_30')
    for key in ('total_return_fraction', 'max_drawdown_signed_fraction', 'sharpe', 'num_trades'):
        a, b = (reported[k][key] for k in ('search', 'detail'))
        if a is not None and b is not None and a != b:
            flags.append('conflicting_reported_' + key)
    candidate['review_flags'] = flags
    # Observation time/title/author/popularity never create a new strategy version.
    candidate['evidence_sha256'] = _material_digest(candidate)
    return candidate


def _material_digest(candidate):
    material = {k: candidate[k] for k in MATERIAL_FIELDS}
    source = candidate['source']
    material['public_reference'] = (source['published_id'], source['slug'])
    return _digest(material)


def _check_complete(complete):
    if (set(complete) != {'kind', 'ordinal', 'started_at', 'finished_at', 'observations',
                         'candidates', 'considered', 'skipped', 'unchanged',
                         'change_unconfirmed', 'more_available'}
            or type(complete['observations']) is not list or len(complete['observations']) > 5
            or type(complete['candidates']) is not list
            or type(complete['more_available']) is not bool
            or any(type(complete[k]) is not int or not 0 <= complete[k] <= 5
                   for k in ('considered', 'skipped', 'unchanged', 'change_unconfirmed'))
            or complete['considered'] != len(complete['observations']) + complete['skipped']):
        raise ValueError
    seen = set()
    for candidate in complete['observations']:
        source = candidate['source']
        identity = _identity(dict(item_type='strategy', published_id=source['published_id'],
                                 slug=source['slug'], app_url=source['url']))
        if (identity[0] in seen or source['provider'] != 'astral_explore'
                or source['provider_version'] is not None
                or candidate['schema'] != 'public_strategy_review_candidate_v1'
                or candidate['origin'] != 'public'
                or candidate['validation_status'] != 'needs_independent_review'
                or candidate['observed_at'] != complete['started_at']
                or any(candidate[k] is not False for k in ('deployment_approved',
                       'overfitting_reviewed', 'reproducibility_verified'))
                or candidate['evidence_sha256'] != _material_digest(candidate)
                or candidate['change_status'] not in ('new_publication', 'observed_material_change',
                       'observed_evidence_unchanged', 'change_unconfirmed')):
            raise ValueError
        seen.add(identity[0])
    if (complete['candidates'] != [c for c in complete['observations']
            if c['change_status'] in ('new_publication', 'observed_material_change')]
            or complete['unchanged'] != sum(c['change_status'] == 'observed_evidence_unchanged'
                                           for c in complete['observations'])
            or complete['change_unconfirmed'] != sum(c['change_status'] == 'change_unconfirmed'
                                                    for c in complete['observations'])):
        raise ValueError


def _journal(directory, now=None):
    store = IndependentPaperLedger(directory)  # reuse private 0700/0600/no-link/fsync checks
    records = {}
    paths = list(store.directory.iterdir())
    if len(paths) > MAX_HISTORY * 2:
        raise ValueError
    for path in paths:
        entry = json.loads(store._read(path), object_pairs_hook=_unique)
        kind, ordinal = entry['kind'], entry['ordinal']
        if (kind not in ('attempt', 'complete') or type(ordinal) is not int or ordinal < 0
                or path != store._path(kind + ':' + str(ordinal)) or (kind, ordinal) in records):
            raise ValueError
        records[kind, ordinal] = entry
    completed = []
    for index in range(len(records) // 2):
        attempt, complete = records['attempt', index], records['complete', index]
        if set(attempt) != {'kind', 'ordinal', 'started_at', 'request', 'verified_us_symbols'}:
            raise ValueError
        _check_complete(complete)
        start, finish = _stamp(attempt['started_at']), _stamp(complete['finished_at'])
        if (complete['started_at'] != attempt['started_at']
                or not timedelta(0) <= finish - start <= timedelta(minutes=5)
                or (completed and start - _stamp(completed[-1]['started_at']) < INTERVAL)
                or (now is not None and finish > now)):
            raise ValueError
        completed.append(complete)
    if len(records) != 2 * len(completed):
        raise ValueError  # incomplete attempts block every later run, including next week
    return store, completed


async def run_public_discovery(call_tool, *, directory, verified_us_symbols, query,
                               enabled=False, mvp_verified=False, clock=None):
    """One bounded newest-first page, at most five detail reads, once per 7 days.

    Both explicit gates default off. They represent caller attestation, not
    evidence generated by this module. No worker/cron/AI process is registered.
    An immutable attempt is fsynced BEFORE external reads. The complete file is
    the candidate/handoff commit; partial responses never produce a handoff.
    Unfinished/failed attempts require manual reconciliation, never auto-retry.
    """
    try:
        if type(enabled) is not bool or type(mvp_verified) is not bool:
            raise ValueError
        if not enabled:
            return DiscoveryBatch('disabled')
        if not mvp_verified or os.getenv('GITHUB_ACTIONS') or not callable(call_tool):
            raise ValueError
        if (type(verified_us_symbols) not in (list, tuple, set, frozenset)
                or not 1 <= len(verified_us_symbols) <= 500
                or any(type(s) is not str or not re.fullmatch(r'[A-Z][A-Z0-9.\-]{0,14}', s)
                       for s in verified_us_symbols)
                or type(query) is not str or len(query) > 120):
            raise ValueError
        clock = clock or (lambda: datetime.now(timezone.utc))
        started = _time(clock())
        journal, completed = _journal(directory, started)
        if completed and started - _stamp(completed[-1]['started_at']) < INTERVAL:
            return DiscoveryBatch('not_due')
        if len(completed) >= MAX_HISTORY:
            raise ValueError
        ordinal = len(completed)
        attempt = dict(kind='attempt', ordinal=ordinal, started_at=started.isoformat(),
            request=dict(query=query, page=1, limit=5, sort_by='newest', sort_dir='desc',
                         types=['strategy']), verified_us_symbols=sorted(set(verified_us_symbols)))
        if not journal._write_once(journal._path('attempt:' + str(ordinal)), _canonical(attempt), ERROR):
            raise ValueError  # common ordinal claim also serializes different clocks/queries
        previous = {c['source']['published_id']: c for batch in completed for c in batch['observations']}

        async def collect():
            search = _body(await call_tool('astral_explore_search', dict(types=['strategy'],
                query=query, sort_by='newest', sort_dir='desc', page=1, limit=5)), 'explore_results')
            items = search['items']
            if (type(items) is not list or len(items) > 5 or type(search.get('count')) is not int
                    or search['count'] != len(items) or search.get('page') != 1
                    or search.get('limit') != 5 or type(search.get('has_more')) is not bool):
                raise ValueError
            observations, candidates, seen = [], [], set()
            skipped = unchanged = unconfirmed = 0
            for item in items:
                public_id, slug = _identity(item)
                if public_id in seen:
                    raise ValueError
                seen.add(public_id)
                if not set(_assets(item)).issubset(set(verified_us_symbols)):
                    skipped += 1
                    continue
                detail = _body(await call_tool('astral_explore_get',
                    dict(item_type='strategy', slug=slug)), 'explore_item')['item']
                candidate = _project(item, detail, started)
                old = previous.get(public_id)
                if old is None:
                    change = 'new_publication'
                elif old['evidence_sha256'] != candidate['evidence_sha256']:
                    change = 'observed_material_change'
                elif candidate['definition_sha256'] is None:
                    change = 'change_unconfirmed'
                    unconfirmed += 1
                else:
                    change = 'observed_evidence_unchanged'
                    unchanged += 1
                candidate['change_status'] = change
                candidate['selection_reasons'] = ['independently_scoped_us_equity_metadata', change,
                                                   'independent_review_required']
                observations.append(candidate)
                if change in ('new_publication', 'observed_material_change'):
                    candidates.append(candidate)
            return observations, candidates, skipped, unchanged, unconfirmed, search['has_more']

        observations, candidates, skipped, unchanged, unconfirmed, more = await asyncio.wait_for(
            collect(), timeout=TIMEOUT_SECONDS)
        finished = _time(clock())
        if not timedelta(0) <= finished - started <= timedelta(minutes=5):
            raise ValueError
        complete = dict(kind='complete', ordinal=ordinal, started_at=started.isoformat(),
            finished_at=finished.isoformat(), observations=observations, candidates=candidates,
            considered=len(observations) + skipped, skipped=skipped, unchanged=unchanged,
            change_unconfirmed=unconfirmed, more_available=more)
        journal._write_once(journal._path('complete:' + str(ordinal)), _canonical(complete), ERROR)
        return DiscoveryBatch('completed_bounded_page', complete['considered'], skipped,
                              unchanged, unconfirmed, more, tuple(candidates))
    except Exception:
        raise DiscoveryError(ERROR) from None


def read_public_candidates(directory):
    """Replay private handoffs after restart; no collection or validation claims.

    Return latest handed-off evidence per publication, marking later incomplete
    change evidence as requiring a source refresh. Never consume/approve a queue.
    """
    try:
        _, completed = _journal(directory)
        pending = {}
        for batch in completed:
            for candidate in batch['candidates']:
                pending[candidate['source']['published_id']] = candidate
            for observation in batch['observations']:
                key = observation['source']['published_id']
                if key in pending:
                    pending[key] = dict(pending[key], needs_source_refresh=
                        observation['change_status'] == 'change_unconfirmed')
        return tuple(pending[k] for k in sorted(pending))
    except Exception:
        raise DiscoveryError(ERROR) from None
