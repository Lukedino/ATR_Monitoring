"""Synthetic compatibility and privacy checks for the shared H/L/C validator."""
from copy import deepcopy
from decimal import Decimal
from itertools import product

import numpy as np
import pandas as pd
import pytest

from price_validation import price_relation_diagnostics, validate_price_frame


def bars(rows=4):
    frame = pd.DataFrame(
        {"High": 11.0, "Low": 9.0, "Close": 10.0, "Open": np.nan, "Volume": 0},
        index=pd.date_range("2025-01-01", periods=rows),
    )
    frame.index.name = "SyntheticDate"
    frame.attrs = {"synthetic": {"source": ["unverified-fixture"]}}
    return frame


def original_validator(df, min_rows=1):
    """Frozen 504774d predicate, independent of the extracted implementation."""
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
    tolerance = prices.abs().max(axis=1) * 1e-7 + 1e-12
    if ((prices.max(axis=1) - prices["High"] > tolerance).any() or
            (prices["Low"] - prices.min(axis=1) > tolerance).any()):
        return None, "inconsistent_prices"
    if len(df) < min_rows:
        return None, "insufficient_history"
    return prices, None


@pytest.mark.parametrize("location,positions", [
    ("history", [0]), ("latest", [3]), ("both", [0, 3]),
])
@pytest.mark.parametrize("values,relations", [
    ((9.0, 11.0, 10.0), {"high_below_low", "close_above_high", "close_below_low"}),
    ((11.0, 9.0, 12.0), {"close_above_high"}),
    ((11.0, 9.0, 8.0), {"close_below_low"}),
])
def test_relation_and_location_enums_preserve_the_complete_input(location, positions, values, relations):
    frame = bars()
    for position in positions:
        frame.iloc[position, :3] = values
    original = frame.copy(deep=True)
    attrs = deepcopy(frame.attrs)
    assert price_relation_diagnostics(frame) == frozenset((relation, location) for relation in relations)
    assert validate_price_frame(frame)[1] == "inconsistent_prices"
    pd.testing.assert_frame_equal(frame, original, check_exact=True)
    assert frame.attrs == attrs


def test_single_row_is_latest_and_multiple_relations_have_independent_locations():
    frame = bars(1)
    frame.loc[frame.index[0], "Close"] = 12.0
    assert price_relation_diagnostics(frame) == frozenset({("close_above_high", "latest")})
    frame = bars()
    frame.loc[frame.index[0], "Close"] = 12.0
    frame.loc[frame.index[-1], "Close"] = 8.0
    assert price_relation_diagnostics(frame) == frozenset({
        ("close_above_high", "history"), ("close_below_low", "latest"),
    })


@pytest.mark.parametrize("scale", [1e-6, 1.0, 1e6, 1e300])
@pytest.mark.parametrize("relation", ["high_below_low", "close_above_high", "close_below_low"])
@pytest.mark.parametrize("fraction,violates", [(0.5, False), (2.0, True)])
def test_both_sides_of_the_unchanged_tolerance(scale, relation, fraction, violates):
    tolerance = scale * 1e-7 + 1e-12
    lower = scale - fraction * tolerance
    values = {
        "high_below_low": (lower, scale, scale),
        "close_above_high": (lower, lower, scale),
        "close_below_low": (scale, scale, lower),
    }[relation]
    frame = pd.DataFrame([values], columns=["High", "Low", "Close"])
    expected = original_validator(frame)[1]
    assert expected == ("inconsistent_prices" if violates else None)
    assert validate_price_frame(frame)[1] == expected
    assert ((relation, "latest") in price_relation_diagnostics(frame)) is violates


def test_tolerance_boundary_adjacent_floats_use_the_original_strict_comparison():
    scale = 1.0
    boundary = scale - (scale * 1e-7 + 1e-12)
    for lower in [np.nextafter(boundary, -np.inf), boundary, np.nextafter(boundary, np.inf)]:
        frame = pd.DataFrame({"High": [lower], "Low": [lower], "Close": [scale]})
        expected = (scale - lower) > (scale * 1e-7 + 1e-12)
        assert validate_price_frame(frame)[1] == ("inconsistent_prices" if expected else None)
        assert bool(price_relation_diagnostics(frame)) == bool(expected)


@pytest.mark.parametrize("defect,reason", [
    ("missing_column", "missing_price_columns"),
    ("duplicate_column", "missing_price_columns"),
    ("duplicate_index", "invalid_index"),
    ("reverse_index", "invalid_index"),
    ("missing_index", "invalid_index"),
    ("multi_index", "invalid_index"),
    ("text", "non_numeric_prices"),
    ("boolean", "non_numeric_prices"),
    ("complex", "non_numeric_prices"),
    ("nan", "non_finite_prices"),
    ("infinity", "non_finite_prices"),
    ("zero", "non_positive_prices"),
    ("negative", "non_positive_prices"),
])
def test_non_relation_failures_take_precedence_and_emit_no_diagnosis(defect, reason):
    frame = bars().astype(object)
    frame.loc[frame.index[-1], "Close"] = 12.0
    if defect == "missing_column":
        frame = frame.drop(columns="High")
    elif defect == "duplicate_column":
        frame = pd.concat([frame, frame[["High"]]], axis=1)
    elif defect == "duplicate_index":
        frame.index = [0, 1, 1, 3]
    elif defect == "reverse_index":
        frame = frame.iloc[::-1]
    elif defect == "missing_index":
        frame.index = pd.DatetimeIndex([pd.NaT, *frame.index[1:]])
    elif defect == "multi_index":
        frame.index = pd.MultiIndex.from_product([[0, 1], [0, 1]])
    else:
        value = {"text": "synthetic-invalid", "boolean": np.bool_(True),
                 "complex": 1 + 2j, "nan": np.nan, "infinity": np.inf,
                 "zero": 0.0, "negative": -1.0}[defect]
        frame.iloc[0, 0] = value
    original = frame.copy(deep=True)
    attrs = deepcopy(frame.attrs)
    assert validate_price_frame(frame, min_rows=100)[1] == reason
    assert price_relation_diagnostics(frame) == frozenset()
    pd.testing.assert_frame_equal(frame, original, check_exact=True)
    assert frame.attrs == attrs


@pytest.mark.parametrize("frame", [None, [], pd.DataFrame()])
def test_empty_and_non_frame_inputs_have_no_diagnostic_details(frame):
    assert validate_price_frame(frame)[1] == "empty_input"
    assert price_relation_diagnostics(frame) == frozenset()


def test_relation_failure_still_precedes_short_history():
    frame = bars(1)
    assert validate_price_frame(frame, min_rows=2)[1] == "insufficient_history"
    assert price_relation_diagnostics(frame) == frozenset()
    frame.loc[frame.index[-1], "Close"] = 12.0
    assert validate_price_frame(frame, min_rows=2)[1] == "inconsistent_prices"


@pytest.mark.parametrize("kind", ["float", "numeric_text", "decimal", "nullable_float", "nullable_int", "nullable_missing"])
def test_supported_numeric_representations_match_the_frozen_validator(kind):
    frame = bars().drop(columns=["Open", "Volume"])
    if kind == "numeric_text":
        frame = frame.astype(str)
    elif kind == "decimal":
        frame = frame.map(lambda value: Decimal(str(value)))
    elif kind == "nullable_float":
        frame = frame.astype("Float64")
    elif kind == "nullable_int":
        frame = frame.astype("Int64")
    elif kind == "nullable_missing":
        frame = frame.astype("Float64")
        frame.iloc[0, 0] = pd.NA
    original = frame.copy(deep=True)
    attrs = deepcopy(frame.attrs)
    expected_prices, expected_issue = original_validator(frame)
    prices, issue = validate_price_frame(frame)
    assert issue == expected_issue
    if prices is not None:
        pd.testing.assert_frame_equal(prices, expected_prices, check_exact=True)
    else:
        assert expected_prices is None
    assert price_relation_diagnostics(frame) == frozenset()
    pd.testing.assert_frame_equal(frame, original, check_exact=True)
    assert frame.attrs == attrs


def test_representative_extreme_relations_equal_the_original_predicate():
    values = [np.nextafter(0.0, 1.0), 1e-12, 1.0, 1.0 + 1e-7, 1e100, np.finfo(float).max]
    for high, low, close in product(values, repeat=3):
        frame = pd.DataFrame({"High": [high], "Low": [low], "Close": [close]})
        expected_prices, expected_issue = original_validator(frame)
        prices, issue = validate_price_frame(frame)
        assert issue == expected_issue
        assert bool(price_relation_diagnostics(frame)) == (issue == "inconsistent_prices")
        if prices is not None:
            pd.testing.assert_frame_equal(prices, expected_prices, check_exact=True)


def test_diagnostics_ignore_open_volume_and_attrs_without_modifying_them():
    frame = bars()
    frame["Open"] = "synthetic-open-not-used"
    frame["Volume"] = "synthetic-volume-not-used"
    original = frame.copy(deep=True)
    attrs = deepcopy(frame.attrs)
    assert price_relation_diagnostics(frame) == frozenset()
    assert validate_price_frame(frame)[1] is None
    pd.testing.assert_frame_equal(frame, original, check_exact=True)
    assert frame.attrs == attrs
