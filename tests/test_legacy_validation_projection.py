"""Preserved historical projection compatibility, not a current collection path."""
from copy import deepcopy

from astral_stock_agent.public_discovery import _project
from test_public_discovery import evidence
from test_independent_validation import NOW, run, checks


def test_historical_projection_is_consumed_unchanged_and_not_source_code_executed():
    summary, detail = evidence()
    c = _project(summary, detail, NOW)
    before = deepcopy(c)
    r = run(origin='public', public_candidate=c)
    assert r.status == 'held' and c == before
    assert checks(r)['source_execution'].reason == 'source_daily_next_open_conditions_differ'

