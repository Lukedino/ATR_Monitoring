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
