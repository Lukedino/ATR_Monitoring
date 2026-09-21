"""Only synthetic Telegram inputs and fake HTTP responses; no actual sends."""
import ast
import importlib.util
from pathlib import Path
import urllib.error
import urllib.parse

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "notify_workflow_failure.py"
SPEC = importlib.util.spec_from_file_location("synthetic_workflow_failure_notice", SOURCE)
notice = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(notice)

FAKE_ENV = {
    "TELEGRAM_BOT_TOKEN": "123456:synthetic_bot_token",
    "TELEGRAM_CHAT_ID": "synthetic_chat",
    "GITHUB_RUN_ID": "123456789",
    "STOCK_LIST": '{"synthetic_private_group": ["SYM-PRIVATE"]}',
    "GOOGLE_SERVICE_ACCOUNT_JSON": "synthetic_private_credentials",
}


class Response:
    def __init__(self, body=b'{"ok": true}', status=200):
        self.body = body
        self.status = status
        self.read_limits = []
        self.closed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.closed = True

    def getcode(self):
        return self.status

    def read(self, limit):
        self.read_limits.append(limit)
        if isinstance(self.body, Exception):
            raise self.body
        return self.body[:limit]


class Opener:
    def __init__(self, response=None, error=None):
        self.response = response if response is not None else Response()
        self.error = error
        self.calls = []

    def open(self, request, *, timeout):
        self.calls.append((request, timeout))
        if self.error:
            raise self.error
        return self.response


def test_notice_is_fixed_text_and_reads_a_bounded_response(capsys):
    opener = Opener()
    assert notice.send_failure_notice(FAKE_ENV, opener=opener) == "sent"
    assert len(opener.calls) == 1
    request, timeout = opener.calls[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://api.telegram.org/bot123456:synthetic_bot_token/sendMessage"
    assert timeout == 10
    fields = urllib.parse.parse_qs(request.data.decode("utf-8"))
    assert set(fields) == {"chat_id", "text"}
    assert fields["chat_id"] == ["synthetic_chat"]
    assert "실행 번호: 123456789" in fields["text"][0]
    for sensitive in ("synthetic_bot_token", "SYM-PRIVATE", "synthetic_private_group", "synthetic_private_credentials"):
        assert sensitive not in fields["text"][0]
    assert opener.response.read_limits == [notice.MAX_RESPONSE_BYTES + 1]
    assert opener.response.closed
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("body,status", [
    (b'{"ok": false}', 200), (b'{"ok": 1}', 200), (b'{"ok": "true"}', 200),
    (b'{}', 200), (b'[]', 200), (b'null', 200), (b'not JSON', 200),
    (b'\xff', 200), (b'{"ok": true}', 201), (b'{"ok": true}', 429),
    (b'{"ok": true}', 500), (b' ' * (notice.MAX_RESPONSE_BYTES + 1), 200),
    (OSError("synthetic_private_response"), 200),
], ids=[
    "ok-false", "ok-integer", "ok-string", "missing-ok", "array", "null",
    "malformed-json", "invalid-encoding", "http-created", "rate-limited",
    "server-error", "oversized-response", "read-error",
])
def test_failed_or_invalid_response_never_reports_success(body, status, capsys):
    opener = Opener(Response(body, status))
    assert notice.send_failure_notice(FAKE_ENV, opener=opener) == "failed"
    assert len(opener.calls) == 1
    assert opener.response.closed
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("error", [
    TimeoutError("synthetic_private_timeout"),
    urllib.error.URLError("https://synthetic.invalid/private?token=synthetic_secret"),
    urllib.error.HTTPError("https://synthetic.invalid/private", 500, "synthetic_secret", {}, None),
])
def test_ambiguous_send_failure_is_not_retried_or_printed(error, capsys):
    opener = Opener(error=error)
    assert notice.send_failure_notice(FAKE_ENV, opener=opener) == "failed"
    assert len(opener.calls) == 1
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("values,expected", [
    ({}, "not_configured"),
    ({"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_CHAT_ID": "synthetic"}, "not_configured"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_CHAT_ID": " "}, "not_configured"),
    ({"TELEGRAM_BOT_TOKEN": None, "TELEGRAM_CHAT_ID": "synthetic"}, "not_configured"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_CHAT_ID": 1}, "not_configured"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic/../send", "TELEGRAM_CHAT_ID": "synthetic"}, "invalid_configuration"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic\nprivate", "TELEGRAM_CHAT_ID": "synthetic"}, "invalid_configuration"),
    ({"TELEGRAM_BOT_TOKEN": "123:" + "x" * 256, "TELEGRAM_CHAT_ID": "synthetic"}, "invalid_configuration"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_CHAT_ID": "synthetic\nprivate"}, "invalid_configuration"),
    ({"TELEGRAM_BOT_TOKEN": "123:synthetic", "TELEGRAM_CHAT_ID": "x" * 129}, "invalid_configuration"),
])
def test_bad_configuration_never_opens_a_connection(values, expected, capsys):
    opener = Opener()
    assert notice.send_failure_notice(values, opener=opener) == expected
    assert not opener.calls
    assert capsys.readouterr() == ("", "")


@pytest.mark.parametrize("run_id", ["synthetic_private_id", "123\nprivate", "1" * 31, None])
def test_invalid_run_identifier_cannot_be_copied_to_message(run_id):
    opener = Opener()
    assert notice.send_failure_notice(dict(FAKE_ENV, GITHUB_RUN_ID=run_id), opener=opener) == "sent"
    fields = urllib.parse.parse_qs(opener.calls[0][0].data.decode("utf-8"))
    assert "실행 번호:" not in fields["text"][0]


@pytest.mark.parametrize("error,expected,exit_code", [
    (None, "sent", 0), (TimeoutError("synthetic_secret"), "failed", 1),
])
def test_main_prints_only_fixed_status_and_uses_non_redirecting_client(monkeypatch, capsys, error, expected, exit_code):
    opener = Opener(error=error)
    handlers = []
    monkeypatch.setattr(notice.urllib.request, "build_opener", lambda handler: (handlers.append(handler), opener)[1])
    assert notice.main(FAKE_ENV) == exit_code
    assert capsys.readouterr() == ("workflow_failure_notice=" + expected + "\n", "")
    assert len(handlers) == 1
    assert isinstance(handlers[0], notice._NoRedirect)
    assert handlers[0].redirect_request(None, None, 302, None, None, "https://synthetic.invalid/") is None


def test_helper_remains_independent_of_config_and_runtime_dependencies():
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add(node.module.split(".")[0])
    assert imports <= {"json", "os", "re", "urllib"}
