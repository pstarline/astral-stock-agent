"""Historical format interpretation, retained only for compatibility.

Independent research/Paper import execution_contracts directly. These aliases
preserve the old public imports; this module grants no service or order access.
"""
from .research_data import ResearchDataError
from .execution_contracts import (
    ExecutionContract, ExecutionParity, LOCAL_DAILY_EXECUTION,
    compare_execution_conditions, require_matching_execution,
)


def inspect_astral_execution(response):
    """Read only a minimal condition projection from strategy_get's response.

    follow_live is configuration data; a true value in a backtest definition
    does not establish that actual live orders exist. Unknown/missing structures
    fail instead of supplying local defaults. No raw response values appear in
    errors. This does not verify signals, sizing, costs, prices or run results.
    """
    try:
        run = response['strategy']['agent_strategy_state']['strategy']
        if (run['t'] != 'run_strategy' or type(run['v']) is not int
                or run['v'] != 2 or run['mode'] != 'backtest'):
            raise ValueError
        engine = run['engine']
        # This is the only external fill mode observed and mapped so far.
        if engine['fill_price_mode'] != 'on_close':
            raise ValueError
        events = engine['events']
        if type(events) is not list:
            raise ValueError
        return ExecutionContract(engine['bar_size'], 'same_bar_close', engine['timezone'],
                                 tuple(events), engine['follow_live'])
    except (KeyError, TypeError, ValueError):
        raise ResearchDataError('unsupported or incomplete Astral execution conditions') from None
