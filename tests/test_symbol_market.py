"""Trading clocks and numeric input ambiguity share a config-free contract."""

from datetime import datetime, timezone

import pytest

from market_dates import market_date, market_timezone
from symbol_market import get_trading_market, is_ambiguous_numeric_symbol


@pytest.mark.parametrize("symbol,market,zone,day", [
    ("SYM-ETF.KS", "KR", "Asia/Seoul", "2026-01-13"),
    ("SYM-ETF.KQ", "KR", "Asia/Seoul", "2026-01-13"),
    (" sym-kr.ks ", "KR", "Asia/Seoul", "2026-01-13"),
    ("SYM-US", "US", "America/New_York", "2026-01-12"),
    ("SYM-US-ETF", "US", "America/New_York", "2026-01-12"),
    ("SYM-COIN-USD", "Crypto", "UTC", "2026-01-13"),
    ("sym-coin-usdt", "Crypto", "UTC", "2026-01-13"),
])
def test_market_and_calendar_use_the_same_symbol_evidence(symbol, market, zone, day):
    now = datetime(2026, 1, 13, 0, 10, tzinfo=timezone.utc)
    assert get_trading_market(symbol) == market
    assert market_timezone(symbol).key == zone
    assert market_date(symbol, now).isoformat() == day


@pytest.mark.parametrize("symbol", ["999991", "991", "000991", "0", "999991.0", "991.000"])
def test_lost_spreadsheet_padding_does_not_establish_a_market(symbol):
    assert is_ambiguous_numeric_symbol(symbol)


@pytest.mark.parametrize("symbol", ["999991.KS", "999991.KQ", "999991-USD", "SYM1", "SYM1-USD"])
def test_explicit_suffixes_and_alphanumeric_tickers_are_not_bare_numbers(symbol):
    assert not is_ambiguous_numeric_symbol(symbol)
