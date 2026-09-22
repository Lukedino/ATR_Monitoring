"""Pure H/L/C validation and value-free relation diagnostics.

No configuration, market providers, state or logging are imported here. Diagnostic
labels describe the supplied frame; they do not certify its source or repair it.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _numeric_price_frame(df):
    """Keep the calculator's validation order and numeric coercion unchanged."""
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None, "empty_input"
    required = ["High", "Low", "Close"]
    if not set(required).issubset(df.columns) or df.columns.has_duplicates:
        return None, "missing_price_columns"
    if (isinstance(df.index, pd.MultiIndex) or df.index.hasnans or
            df.index.has_duplicates or not df.index.is_monotonic_increasing):
        return None, "invalid_index"
    columns = required
    try:
        numeric = df[columns].apply(pd.to_numeric, errors="raise")
        if (any(np.iscomplexobj(numeric[column]) for column in columns) or
                df[columns].apply(lambda column: column.map(
                    lambda value: isinstance(value, (bool, np.bool_)))).any().any()):
            return None, "non_numeric_prices"
        prices = numeric.astype(float)
    except (ValueError, TypeError, OverflowError):
        return None, "non_numeric_prices"
    if not np.isfinite(prices.to_numpy()).all():
        return None, "non_finite_prices"
    if (prices <= 0).any().any():
        return None, "non_positive_prices"
    return prices, None


def _price_tolerance(prices):
    return prices.abs().max(axis=1) * 1e-7 + 1e-12


def validate_price_frame(df: pd.DataFrame, min_rows: int = 1):
    """Validate ATR's H/L/C history without filling or dropping price bars.

    Open is not an ATR/Chandelier input. Preserve the established relation
    predicate and its precedence over the minimum-history check.
    """
    prices, issue = _numeric_price_frame(df)
    if issue:
        return None, issue
    tolerance = _price_tolerance(prices)
    if ((prices.max(axis=1) - prices["High"] > tolerance).any() or
            (prices["Low"] - prices.min(axis=1) > tolerance).any()):
        return None, "inconsistent_prices"
    if len(df) < min_rows:
        return None, "insufficient_history"
    return prices, None


def price_relation_diagnostics(df) -> frozenset[tuple[str, str]]:
    """Return only present relation/location enums, never input values or keys.

    ``latest`` is the frame's final row, not a claim about market freshness.
    Non-relation input failures produce no relation diagnosis. Neither the frame
    nor its index, columns or attrs are changed.
    """
    prices, issue = _numeric_price_frame(df)
    if issue:
        return frozenset()
    tolerance = _price_tolerance(prices)
    relations = (
        ("high_below_low", prices["Low"] - prices["High"] > tolerance),
        ("close_above_high", prices["Close"] - prices["High"] > tolerance),
        ("close_below_low", prices["Low"] - prices["Close"] > tolerance),
    )
    result = set()
    for relation, mask in relations:
        history = bool(mask.iloc[:-1].any())
        latest = bool(mask.iloc[-1])
        if history or latest:
            location = "both" if history and latest else "history" if history else "latest"
            result.add((relation, location))
    return frozenset(result)
