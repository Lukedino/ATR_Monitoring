"""Market calendar dates for data and alerts, independent of the host timezone.

Daily candle indexes are date labels, not instants to convert to another zone.
This module does not decide whether a market is open or a quote is recent enough.
"""

from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from symbol_market import get_trading_market


def utc_now(now_utc: datetime | None = None) -> datetime:
    """Return an aware UTC instant; reject ambiguous injected naive clocks."""
    if now_utc is None:
        return datetime.now(timezone.utc)
    if (
        not isinstance(now_utc, datetime)
        or now_utc != now_utc
        or now_utc.utcoffset() is None
    ):
        raise ValueError("Current time must be a timezone-aware datetime")
    return now_utc.astimezone(timezone.utc)


def market_timezone(symbol: str) -> ZoneInfo:
    """Use Seoul for KR suffixes, UTC for crypto pairs, New York otherwise.

    KR suffixes take precedence over ETF classification. This affects dates
    only and does not change ATR multipliers or market activation policy.
    """
    zone = {"KR": "Asia/Seoul", "Crypto": "UTC", "US": "America/New_York"}
    return ZoneInfo(zone[get_trading_market(symbol)])


def market_date(symbol: str, now_utc: datetime | None = None) -> date:
    """Return the symbol's calendar date at one unambiguous instant."""
    return utc_now(now_utc).astimezone(market_timezone(symbol)).date()


def daily_bar_date(value: object) -> date | None:
    """Read a daily bar's displayed date without shifting its timezone.

    Numeric indexes, missing dates and unparseable labels have no date. Pandas
    Timestamp subclasses datetime; NaT is rejected by its unequal-to-self rule.
    """
    if isinstance(value, (datetime, date)):
        if value != value:
            return None
        result = value.date() if isinstance(value, datetime) else value
        return result if isinstance(result, date) else None
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            try:
                return datetime.fromisoformat(value).date()
            except ValueError:
                return None
    return None
