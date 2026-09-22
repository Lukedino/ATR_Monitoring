"""Presence-only collection diagnostics; never retain an item identity or frame.

Each batch emits at most one message composed entirely of allowlisted codes.
The stages describe where a relation was observed, not its cause. A repeated
relation in another stage is still useful even when its history/latest scope
changes. Observation never determines whether collection or calculation succeeds.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import logging

from price_validation import price_relation_diagnostics


logger = logging.getLogger(__name__)

_STAGES = frozenset({"history_normalized", "raw_fallback", "latest_quote"})
_RELATIONS = frozenset({"high_below_low", "close_above_high", "close_below_low"})
_SCOPES = frozenset({"history", "latest", "both"})
_OBSERVATIONS = frozenset({"first_observed", "already_observed"})
_UNAVAILABLE = "diagnostics_unavailable"
_CODES = frozenset(
    f"{stage}.{relation}.{scope}.{observation}"
    for stage in _STAGES
    for relation in _RELATIONS
    for scope in _SCOPES
    for observation in _OBSERVATIONS
) | {_UNAVAILABLE}

# Immutable values also isolate copied contexts. No frame, symbol, date, count,
# position, or exception is stored in either context variable.
_batch_codes: ContextVar[frozenset[str] | None] = ContextVar(
    "price_diagnostic_batch_codes", default=None
)
_item_seen: ContextVar[frozenset[str] | None] = ContextVar(
    "price_diagnostic_item_relations", default=None
)


def _unavailable() -> None:
    try:
        codes = _batch_codes.get()
        if codes is not None:
            _batch_codes.set(codes | {_UNAVAILABLE})
    except Exception:
        pass


def _emit(codes: frozenset[str]) -> None:
    try:
        # Revalidate at the only output boundary. No arbitrary values are
        # interpolated, including malformed diagnostic results or exceptions.
        safe = sorted(code for code in codes if type(code) is str and code in _CODES)
        if safe:
            logger.warning("price_relation_diagnostics %s", ";".join(safe))
    except Exception:
        # Logging failure must not replace a collection result or exception.
        pass


@contextmanager
def price_diagnostic_batch():
    """Isolate one batch, restore its enclosing context, then emit presence."""
    batch_token = _batch_codes.set(frozenset())
    item_token = _item_seen.set(None)
    try:
        yield
    finally:
        codes = _batch_codes.get()
        _item_seen.reset(item_token)
        _batch_codes.reset(batch_token)
        _emit(codes)


@contextmanager
def price_diagnostic_item():
    """Track only fixed relation codes during a single collection call."""
    token = _item_seen.set(frozenset())
    try:
        yield
    finally:
        _item_seen.reset(token)


def observe_price_stage(stage: str, df) -> None:
    """Observe a frame without mutation; direct fetches outside a batch do none."""
    try:
        codes = _batch_codes.get()
        seen = _item_seen.get()
        if codes is None or seen is None:
            return
        if type(stage) is not str or stage not in _STAGES:
            _unavailable()
            return
        relations = price_relation_diagnostics(df)
        if type(relations) is not frozenset:
            _unavailable()
            return
        observed = set()
        additions = set()
        for pair in relations:
            if (type(pair) is not tuple or len(pair) != 2
                    or any(type(part) is not str for part in pair)):
                _unavailable()
                return
            relation, scope = pair
            if relation not in _RELATIONS or scope not in _SCOPES:
                _unavailable()
                return
            observation = "already_observed" if relation in seen else "first_observed"
            additions.add(f"{stage}.{relation}.{scope}.{observation}")
            observed.add(relation)
        _batch_codes.set(codes | additions)
        _item_seen.set(seen | observed)
    except Exception:
        _unavailable()
