"""Synthetic compact windows: rollover, restart, revisions and private storage."""
from dataclasses import replace
from datetime import datetime, timezone
import json

import pytest

from astral_stock_agent.market_data import normalize_alpha_daily, ROW_FIELDS
from astral_stock_agent.price_archive import PriceArchive, PriceArchiveError


DAYS = ('2026-01-02', '2026-01-05', '2026-01-06', '2026-01-07', '2026-01-08')


def window(start, end, changed=None):
    now = datetime.fromisoformat(DAYS[end-1]+'T22:00:00+00:00')
    series = {day: dict(zip(ROW_FIELDS, ('10','12','9','11','100')))
              for day in DAYS[start:end]}
    if changed:
        series[changed]['4. close'] = '10'
    raw = json.dumps({'Meta Data': {
        '1. Information': 'Daily Prices (open, high, low, close) and Volumes',
        '2. Symbol': 'IBM', '3. Last Refreshed': DAYS[end-1],
        '4. Output Size': 'Compact', '5. Time Zone': 'US/Eastern'},
        'Time Series (Daily)': series}).encode()
    return normalize_alpha_daily(raw, symbol='IBM', verified_us_symbols=['IBM'], now=now), now


@pytest.fixture
def archive(tmp_path, monkeypatch):
    monkeypatch.delenv('GITHUB_ACTIONS', raising=False)
    path = tmp_path/'prices'
    path.mkdir(mode=0o700)
    return PriceArchive(path), path


def append(store, start, end, *, bootstrap=False, changed=None):
    data, now = window(start, end, changed)
    return store.append(data, verified_us_symbols=['IBM'], now=now, bootstrap=bootstrap)


def test_rolling_window_retains_fixed_start_across_restart(archive):
    store, path = archive
    first = append(store, 0, 3, bootstrap=True)
    second = append(PriceArchive(path), 1, 4)
    third = append(PriceArchive(path), 2, 5)
    assert first.count == 3 and second.count == 4 and third.count == 5
    assert third.first_day.isoformat() == DAYS[0]
    assert third.csv_bytes.startswith(first.csv_bytes)
    assert 'timestamp,open' not in repr(third)
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in path.iterdir())
    repeat = append(PriceArchive(path), 2, 5)
    assert repeat.created is False and repeat.csv_bytes == third.csv_bytes


@pytest.mark.parametrize('changed', DAYS[1:3])
def test_revision_blocks_before_creating_next_snapshot(archive, changed):
    store, path = archive
    append(store, 0, 3, bootstrap=True)
    before = {p.name:p.read_bytes() for p in path.iterdir()}
    with pytest.raises(PriceArchiveError):
        append(store, 1, 4, changed=changed)
    assert before == {p.name:p.read_bytes() for p in path.iterdir()}


def test_gap_and_missing_previous_do_not_bootstrap_silently(archive):
    store, _ = archive
    with pytest.raises(PriceArchiveError):
        append(store, 1, 4)
    append(store, 0, 3, bootstrap=True)
    with pytest.raises(PriceArchiveError):
        append(store, 3, 5)  # no exact preceding daily snapshot


def test_no_overlap_and_bootstrap_reset_are_blocked(archive):
    store, _ = archive
    append(store, 0, 3, bootstrap=True)
    with pytest.raises(PriceArchiveError):
        append(store, 3, 4)
    with pytest.raises(PriceArchiveError):
        append(store, 1, 4, bootstrap=True)


@pytest.mark.parametrize('field,value', [('symbol','SPY'), ('csv_sha256','bad'),
    ('count',2), ('provider','other'), ('price_basis','adjusted'),
    ('provider_authenticity_verified',True)])
def test_constructed_object_cannot_bypass_scope_or_integrity(archive, field, value):
    store, path = archive
    data, now = window(0,3)
    with pytest.raises(PriceArchiveError):
        store.append(replace(data, **{field:value}), verified_us_symbols=['IBM'],
                     now=now, bootstrap=True)
    assert not list(path.iterdir())


def test_truncated_snapshot_blocks_restart(archive):
    store, path = archive
    append(store, 0, 3, bootstrap=True)
    checkpoint = store._storage._path('price-archive:'+DAYS[2])
    checkpoint.write_bytes(b'{')
    with pytest.raises(PriceArchiveError):
        append(PriceArchive(path), 1, 4)
    assert checkpoint.read_bytes() == b'{'


def test_public_ci_blocks_storage(archive, monkeypatch):
    store, path = archive
    monkeypatch.setenv('GITHUB_ACTIONS','true')
    with pytest.raises(PriceArchiveError):
        append(store, 0, 3, bootstrap=True)
    assert not list(path.iterdir())


def test_archive_grows_past_compact_limit_without_dropping_first_session(archive):
    from datetime import date, timedelta
    from astral_stock_agent.sessions import session_on
    store, _ = archive
    days = []
    day = date(2026,1,2)
    while len(days) < 101:
        if session_on(day):
            days.append(day.isoformat())
        day += timedelta(days=1)
    for index, selected in enumerate((days[:100], days[1:])):
        series = {d:dict(zip(ROW_FIELDS, ('10','12','9','11','100'))) for d in selected}
        raw = json.dumps({'Meta Data':{
            '1. Information':'Daily Prices (open, high, low, close) and Volumes',
            '2. Symbol':'IBM','3. Last Refreshed':selected[-1],
            '4. Output Size':'Compact','5. Time Zone':'US/Eastern'},
            'Time Series (Daily)':series}).encode()
        now = datetime.fromisoformat(selected[-1]+'T22:00:00+00:00')
        data = normalize_alpha_daily(raw,symbol='IBM',verified_us_symbols=['IBM'],now=now)
        result = store.append(data,verified_us_symbols=['IBM'],now=now,bootstrap=index==0)
    assert result.count == 101 and result.first_day.isoformat() == days[0]
    assert len(result.csv_bytes.splitlines()) == 102
