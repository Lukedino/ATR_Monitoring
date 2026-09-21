"""Import the actual config with synthetic environment and fake Drive only."""
import importlib.util
import json
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


SYNTHETIC_LIST = {"synthetic": ["SYM-QC1", "999991.KS", "FAKE-USD"]}
SYNTHETIC_CSV = "Ticker,구분,계좌,종목\nSYM-DRIVE,미국,synthetic-account,synthetic-name\n".encode()


@pytest.fixture
def load_config(monkeypatch):
    def load(values=None, *, csv=SYNTHETIC_CSV, drive_error=None):
        calls = []
        with monkeypatch.context() as context:
            # No inherited account, credential, portfolio, path or dotenv value.
            context.setattr(os, "environ", dict(values or {}))
            dotenv = ModuleType("dotenv")
            dotenv.load_dotenv = lambda *args, **kwargs: None
            context.setitem(sys.modules, "dotenv", dotenv)

            class Call:
                def __init__(self, value):
                    self.value = value

                def execute(self):
                    if drive_error:
                        raise drive_error
                    return self.value

            class Files:
                def get(self, **kwargs):
                    calls.append("metadata")
                    return Call({"mimeType": "text/csv"})

                def get_media(self, **kwargs):
                    calls.append("csv")
                    return Call(csv)

            def credentials(info, **kwargs):
                assert info == {"synthetic": True}
                calls.append("credentials")
                return object()

            def build(*args, **kwargs):
                calls.append("build")
                return SimpleNamespace(files=lambda: Files())

            google = ModuleType("google")
            oauth2 = ModuleType("google.oauth2")
            service_account = ModuleType("google.oauth2.service_account")
            service_account.Credentials = SimpleNamespace(from_service_account_info=credentials)
            oauth2.service_account = service_account
            google.oauth2 = oauth2
            api = ModuleType("googleapiclient")
            discovery = ModuleType("googleapiclient.discovery")
            discovery.build = build
            api.discovery = discovery
            for name, module in (("google", google), ("google.oauth2", oauth2),
                                 ("google.oauth2.service_account", service_account),
                                 ("googleapiclient", api), ("googleapiclient.discovery", discovery)):
                context.setitem(sys.modules, name, module)

            path = Path(__file__).resolve().parents[1] / "config.py"
            spec = importlib.util.spec_from_file_location("synthetic_portfolio_config", path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        return module, calls
    return load


def assert_invalid(module, error=None):
    assert module.PORTFOLIO == {}
    assert module.ALL_SYMBOLS == []
    assert module.KR_SYMBOLS == module.CRYPTO_SYMBOLS == module.US_SYMBOLS == []
    assert module.PORTFOLIO_SOURCE == "invalid"
    assert module.PORTFOLIO_ERROR
    if error:
        assert module.PORTFOLIO_ERROR == error
    assert module.SYMBOL_ACCOUNTS == {}
    assert module.SYMBOL_ENTRY_PRICES == {}
    assert module._kr_names_from_drive == {}


def test_unconfigured_local_development_retains_demo_fallback(load_config):
    module, calls = load_config()
    assert module.PORTFOLIO_SOURCE == "fallback"
    assert module.PORTFOLIO == module._PORTFOLIO_FALLBACK
    assert module.PORTFOLIO is not module._PORTFOLIO_FALLBACK
    assert module.ALL_SYMBOLS
    assert module.PORTFOLIO_ERROR == ""
    assert calls == []


def test_ci_without_portfolio_never_uses_local_demo(load_config):
    module, calls = load_config({"GITHUB_ACTIONS": "true"})
    assert_invalid(module, "portfolio_source_missing")
    assert calls == []


@pytest.mark.parametrize("in_ci", [False, True])
def test_valid_legacy_list_has_matching_source_and_no_drive_access(load_config, in_ci):
    module, calls = load_config({"GITHUB_ACTIONS": str(in_ci), "STOCK_LIST": json.dumps(SYNTHETIC_LIST)})
    assert module.PORTFOLIO == SYNTHETIC_LIST
    assert module.PORTFOLIO_SOURCE == "stock_list"
    assert module.PORTFOLIO_ERROR == ""
    assert module.ALL_SYMBOLS == SYNTHETIC_LIST["synthetic"]
    assert calls == []


@pytest.mark.parametrize("value", [
    None, [], ["SYM-QC1"], True, 123, "SYM-QC1", {}, {"synthetic": []},
    {"synthetic": "SYM-QC1"}, {"synthetic": None}, {"synthetic": {}},
    {"synthetic": [123]}, {"synthetic": [False]}, {"synthetic": [float("nan")]},
    {"synthetic": [float("inf")]}, {"synthetic": [{}]}, {"synthetic": [[]]},
    {"synthetic": [""]}, {"synthetic": [" "]}, {"synthetic": ["SYM-QC1 "]},
    {"synthetic": ["SYM\nQC1"]}, {"": ["SYM-QC1"]},
    {"synthetic": ["SYM-QC1"], "other": [None]},
])
def test_invalid_legacy_schema_never_becomes_a_demo_or_partial_list(load_config, value):
    module, calls = load_config({"GITHUB_ACTIONS": "true", "STOCK_LIST": json.dumps(value)})
    assert_invalid(module, "stock_list_invalid")
    assert calls == []


@pytest.mark.parametrize("raw", ["{broken", " ", '{"synthetic": ["SYM-QC1"]} trailing'])
@pytest.mark.parametrize("in_ci", [False, True])
def test_explicit_invalid_json_never_falls_back_even_locally(load_config, raw, in_ci):
    module, calls = load_config({"GITHUB_ACTIONS": str(in_ci), "STOCK_LIST": raw})
    assert_invalid(module, "stock_list_invalid")
    assert calls == []


@pytest.mark.parametrize("raw", [
    '{"synthetic": ["SYM-FIRST"], "synthetic": ["SYM-SECOND"]}',
    r'{"synthetic": ["SYM-FIRST"], "\u0073ynthetic": ["SYM-SECOND"]}',
])
def test_duplicate_json_group_never_discards_earlier_symbols(load_config, raw):
    module, calls = load_config({"GITHUB_ACTIONS": "true", "STOCK_LIST": raw})
    assert_invalid(module, "stock_list_invalid")
    assert calls == []


@pytest.mark.parametrize("credential,file_id,expected", [
    (False, False, "stock_list"), (True, False, "stock_list"),
    (False, True, "invalid"), (True, True, "drive"),
])
def test_partial_drive_configuration_preserves_valid_state_only_credentials(load_config, credential, file_id, expected):
    values = {"GITHUB_ACTIONS": "true", "STOCK_LIST": json.dumps(SYNTHETIC_LIST)}
    if credential:
        values["GOOGLE_SERVICE_ACCOUNT_JSON"] = '{"synthetic": true}'
    if file_id:
        values["GDRIVE_PORTFOLIO_FILE_ID"] = "synthetic-file"
    module, calls = load_config(values)
    assert module.PORTFOLIO_SOURCE == expected
    assert module.DRIVE_PORTFOLIO_CONFIGURED == file_id
    if expected == "invalid":
        assert_invalid(module, "drive_portfolio_configuration_incomplete")
    elif expected == "drive":
        assert module.ALL_SYMBOLS == ["SYM-DRIVE"]
        assert module.SYMBOL_ACCOUNTS == {"SYM-DRIVE": ["synthetic-account"]}
        assert module.PORTFOLIO_ERROR == ""
    assert bool(calls) == (credential and file_id)


@pytest.mark.parametrize("in_ci", [False, True])
def test_credential_without_portfolio_id_or_legacy_list_is_not_demo_mode(load_config, in_ci):
    module, calls = load_config({"GITHUB_ACTIONS": str(in_ci), "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}'})
    assert_invalid(module, "portfolio_source_missing")
    assert calls == []


def test_drive_failure_does_not_use_legacy_list_or_expose_exception_values(load_config, caplog):
    module, calls = load_config({
        "GITHUB_ACTIONS": "true", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
        "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file", "STOCK_LIST": json.dumps(SYNTHETIC_LIST),
    }, drive_error=RuntimeError("SYNTHETIC-PRIVATE-URL-AND-TOKEN"))
    assert_invalid(module, "drive_portfolio_unavailable_or_invalid")
    assert calls == ["credentials", "build", "metadata"]
    assert "SYNTHETIC-PRIVATE" not in caplog.text
    assert "fallback" not in caplog.text


def test_empty_drive_portfolio_never_uses_legacy_or_demo(load_config):
    module, _ = load_config({
        "GITHUB_ACTIONS": "true", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
        "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file", "STOCK_LIST": json.dumps(SYNTHETIC_LIST),
    }, csv=b"Ticker\n")
    assert_invalid(module, "drive_portfolio_unavailable_or_invalid")


def test_valid_drive_remains_preferred_over_invalid_unused_legacy_input(load_config):
    module, calls = load_config({
        "GITHUB_ACTIONS": "true", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
        "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file", "STOCK_LIST": "{broken",
    })
    assert module.PORTFOLIO_SOURCE == "drive"
    assert module.ALL_SYMBOLS == ["SYM-DRIVE"]
    assert module.PORTFOLIO_ERROR == ""
    assert calls == ["credentials", "build", "metadata", "csv"]


@pytest.mark.parametrize("symbol", ["999991", "991", "000991", "999991.0", "000991.00", "0", "9999999"])
@pytest.mark.parametrize("in_ci", [False, True])
def test_bare_numeric_legacy_symbol_invalidates_the_whole_source(load_config, symbol, in_ci, caplog):
    module, calls = load_config({
        "GITHUB_ACTIONS": str(in_ci),
        "STOCK_LIST": json.dumps({"synthetic": ["SYM-FIRST", symbol, "SYM-LAST"]}),
    })
    assert_invalid(module, "stock_list_invalid")
    assert calls == []
    assert "SYM-FIRST" not in caplog.text and "SYM-LAST" not in caplog.text


@pytest.mark.parametrize("symbol,category", [
    ("999991", "ETF"), ("991", "ETF"), ("999991.0", "ETF"),
    ("000991.00", "ETF"), ("999991", "미국"), ("999991", ""),
    ("999991", "synthetic-unknown"),
])
@pytest.mark.parametrize("in_ci", [False, True])
def test_numeric_drive_row_never_leaves_a_partial_portfolio_or_fallback(load_config, symbol, category, in_ci, caplog):
    csv = (
        "Ticker,구분,계좌,종목,진입가격\n"
        "SYM-FIRST,미국,synthetic-private-account,synthetic-private-name,123\n"
        f"{symbol},{category},synthetic-private-account,synthetic-private-name,456\n"
        "SYM-LAST,미국,synthetic-private-account,synthetic-private-name,789\n"
    ).encode()
    module, calls = load_config({
        "GITHUB_ACTIONS": str(in_ci), "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
        "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file", "STOCK_LIST": json.dumps(SYNTHETIC_LIST),
    }, csv=csv)
    assert_invalid(module, "drive_portfolio_unavailable_or_invalid")
    assert calls == ["credentials", "build", "metadata", "csv"]
    assert "synthetic-private" not in caplog.text
    assert "SYM-FIRST" not in caplog.text and "SYM-LAST" not in caplog.text
    assert symbol not in caplog.text


@pytest.mark.parametrize("ticker,category,expected,venue", [
    ("991", "한국", "000991.KS", "KR"),
    ("991", "코스닥", "000991.KQ", "KR"),
    ("991.0", "한국", "000991.KS", "KR"),
    ("991.00", "코스닥", "000991.KQ", "KR"),
    ("000991", "한국", "000991.KS", "KR"),
    ("999991.KS", "ETF", "999991.KS", "KR"),
    ("999991.KQ", "ETF", "999991.KQ", "KR"),
    ("999991.KQ", "한국", "999991.KQ", "KR"),
    ("999991.ks", "코스닥", "999991.ks", "KR"),
    ("SYM-US", "ETF", "SYM-US", "US"),
    ("SYM-COIN", "크립토", "SYM-COIN-USD", "Crypto"),
    ("SYM-COIN-USDT", "미국", "SYM-COIN-USDT", "Crypto"),
])
def test_explicit_venue_and_existing_category_normalization_remain_valid(load_config, ticker, category, expected, venue):
    csv = f"Ticker,구분,계좌,종목,진입가격\n{ticker},{category},synthetic-account,synthetic-name,123\n".encode()
    module, _ = load_config({
        "GITHUB_ACTIONS": "true", "GOOGLE_SERVICE_ACCOUNT_JSON": '{"synthetic": true}',
        "GDRIVE_PORTFOLIO_FILE_ID": "synthetic-file",
    }, csv=csv)
    assert module.PORTFOLIO_SOURCE == "drive"
    assert module.PORTFOLIO_ERROR == ""
    assert module.ALL_SYMBOLS == [expected]
    assert module.get_trading_market(expected) == venue
    assert module.SYMBOL_ACCOUNTS == {expected: ["synthetic-account"]}
    assert module.SYMBOL_ENTRY_PRICES == {expected: 123.0}
    assert module._kr_names_from_drive == {expected: "synthetic-name"}
    assert module.KR_SYMBOLS == ([expected] if venue == "KR" else [])
    assert module.US_SYMBOLS == ([expected] if venue == "US" else [])
    assert module.CRYPTO_SYMBOLS == ([expected] if venue == "Crypto" else [])


def test_legacy_groups_do_not_assign_venue_or_change_valid_symbol_spelling(load_config):
    portfolio = {"ETF": ["999991.KS", "999992.kq", "SYM-ETF"],
                 "synthetic": ["SYM-COIN-USD", "SYM-NUM123"]}
    module, calls = load_config({"STOCK_LIST": json.dumps(portfolio), "GITHUB_ACTIONS": "true"})
    assert module.PORTFOLIO == portfolio
    assert module.KR_SYMBOLS == ["999991.KS", "999992.kq"]
    assert module.US_SYMBOLS == ["SYM-ETF", "SYM-NUM123"]
    assert module.CRYPTO_SYMBOLS == ["SYM-COIN-USD"]
    assert module.PORTFOLIO_ERROR == ""
    assert calls == []


def test_trading_market_is_separate_from_existing_etf_strategy_group(load_config, monkeypatch):
    module, _ = load_config({"STOCK_LIST": json.dumps(SYNTHETIC_LIST)})
    monkeypatch.setattr(module, "_ETF_SYMBOLS", frozenset({"999991.KS", "SYM-ETF"}))
    # Deliberately distinct synthetic baselines make an accidental venue-based
    # strategy change visible even though the real KR/ETF defaults are equal.
    monkeypatch.setattr(module, "ATR_MULTIPLE_BY_MARKET", {"KR": 3.0, "US": 4.0, "ETF": 2.0, "Crypto": 5.0})
    assert module.get_market_type("999991.KS") == "ETF"
    assert module.get_trading_market("999991.KS") == "KR"
    assert module.get_market_type("SYM-ETF") == "ETF"
    assert module.get_trading_market("SYM-ETF") == "US"
    for symbol in ("999991.KS", "SYM-ETF"):
        assert module.get_atr_multiple(symbol) == 2.0
        assert module.get_atr_multiple(symbol, 6.99) == 2.0
        assert module.get_atr_multiple(symbol, 7.0) == 1.75
        assert module.get_atr_multiple(symbol, 10.0) == 1.5
    assert module.get_atr_multiple("999992.KS") == 3.0
    assert module.get_atr_multiple("SYM-US") == 4.0
    assert module.get_atr_multiple("SYM-COIN-USD") == 5.0


def test_common_validator_rejects_ambiguous_numeric_with_fixed_error(load_config):
    module, _ = load_config({"STOCK_LIST": json.dumps(SYNTHETIC_LIST)})
    with pytest.raises(ValueError, match="^portfolio_symbol_market_required$"):
        module._validated_portfolio({"synthetic-private-group": ["SYM-FIRST", "999991.0"]})
