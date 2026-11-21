import pytest


@pytest.fixture
def historical_platform_contract(monkeypatch):
    """Exercise ONLY pre-retirement synthetic contracts, not current admission.

    No production flag exists. Default-admission tests live in a separate module
    without this fixture. Remove these historical callbacks with later cleanup.
    """
    from astral_stock_agent import retired_platform
    monkeypatch.setattr(retired_platform, 'block', lambda: None)
