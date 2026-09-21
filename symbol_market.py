"""Trading market for validated symbols, separate from ATR asset classes.

ETF membership must not decide which market clock applies. Numeric symbols
without a market suffix are rejected at the portfolio input boundary.
"""

import re


def get_trading_market(symbol: str) -> str:
    """Return KR, Crypto or US using the supported symbol suffixes.

    Unqualified nonnumeric symbols retain the existing US-market default.
    This helper does not determine ETF membership or an ATR multiplier.
    """
    normalized = symbol.strip().upper()
    if normalized.endswith((".KS", ".KQ")):
        return "KR"
    if normalized.endswith(("-USD", "-USDT")):
        return "Crypto"
    return "US"


def is_ambiguous_numeric_symbol(symbol: str) -> bool:
    """A bare integer or spreadsheet-style integral decimal has no market.

    Do not infer a venue or add a suffix here. Explicit category normalization
    happens first in the portfolio parser; explicit suffixes remain intact.
    """
    return re.fullmatch(r"[0-9]+(?:\.0+)?", symbol) is not None
