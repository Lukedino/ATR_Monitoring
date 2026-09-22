"""명시적 Telegram 수신 확인만 상태 기록으로 연결한다(외부 전송 없음)."""
import json

import pytest
import requests

import monitor
import stop_manager
import telegram_bot as telegram
from test_silent_failures import env, _market


PRIVATE = "SYNTHETIC-PRIVATE-RESPONSE"


def response(status=200, payload=None, *, raw=None):
    result = requests.Response()
    result.status_code = status
    result.url = "https://example.invalid/" + PRIVATE
    result._content = raw if raw is not None else json.dumps(payload).encode()
    return result


@pytest.fixture
def transport(monkeypatch):
    calls, sleeps = [], []
    monkeypatch.setattr(telegram, "_is_configured", lambda: True)
    monkeypatch.setattr(telegram.time, "sleep", sleeps.append)
    def install(outcomes):
        values = iter(outcomes)
        def post(*args, **kwargs):
            calls.append(kwargs)
            value = next(values)
            if isinstance(value, Exception):
                raise value
            return value
        monkeypatch.setattr(telegram.requests, "post", post)
    return install, calls, sleeps


@pytest.mark.parametrize("method", ["message", "photo"])
@pytest.mark.parametrize("payload", [None, {}, [], {"ok": False}, {"ok": 1}, {"ok": "true"},
                                     {"ok": None}, {"description": PRIVATE}])
def test_http_200_without_boolean_true_is_unconfirmed_once(transport, caplog, method, payload):
    install, calls, sleeps = transport
    install([response(payload=payload)])
    send = telegram.send_message if method == "message" else telegram.send_photo
    assert send("synthetic text" if method == "message" else b"synthetic png") is False
    assert len(calls) == 1 and sleeps == []
    assert PRIVATE not in caplog.text and "telegram_response_unconfirmed" in caplog.text


@pytest.mark.parametrize("method", ["message", "photo"])
@pytest.mark.parametrize("raw", [b"", PRIVATE.encode(), b'{"ok":'])
def test_empty_or_non_json_receipt_does_not_trigger_new_retry(transport, caplog, method, raw):
    install, calls, sleeps = transport
    install([response(raw=raw)])
    assert (telegram.send_message("synthetic") if method == "message" else telegram.send_photo(b"png")) is False
    assert len(calls) == 1 and sleeps == [] and PRIVATE not in caplog.text


@pytest.mark.parametrize("method", ["message", "photo"])
@pytest.mark.parametrize("status", [201, 204, 302])
def test_non_200_http_cannot_be_certified_by_ok_true(transport, method, status):
    install, calls, sleeps = transport
    install([response(status, {"ok": True})])
    assert (telegram.send_message("synthetic") if method == "message" else telegram.send_photo(b"png")) is False
    assert len(calls) == 1 and sleeps == []


@pytest.mark.parametrize("method", ["message", "photo"])
def test_ok_true_requires_no_additional_message_schema(transport, method):
    install, calls, sleeps = transport
    install([response(payload={"ok": True})])
    assert (telegram.send_message("synthetic") if method == "message" else telegram.send_photo(b"png")) is True
    assert len(calls) == 1 and sleeps == []
    assert calls[0]["timeout"] == telegram.TIMEOUT_SEC == 20


def test_markdown_400_fallback_keeps_payload_and_attempt_policy(transport):
    install, calls, sleeps = transport
    install([response(400, {"description": PRIVATE}), response(payload={"ok": True})])
    assert telegram.send_message("*synthetic*_text`") is True
    assert len(calls) == 2 and sleeps == []
    assert calls[0]["json"]["parse_mode"] == "Markdown"
    assert "parse_mode" not in calls[1]["json"]
    assert calls[1]["json"]["text"] == "synthetictext"


def test_existing_text_rate_limit_wait_cap_and_three_attempt_limit(transport):
    install, calls, sleeps = transport
    install([response(429, {"parameters": {"retry_after": 80}}) for _ in range(3)])
    assert telegram.send_message("synthetic") is False
    assert len(calls) == 3 and sleeps == [30, 30, 30]


@pytest.mark.parametrize("wait", [1, 45.5])
def test_photo_rate_limit_keeps_minimum_30_seconds_and_three_attempts(transport, wait):
    install, calls, sleeps = transport
    install([response(429, {"parameters": {"retry_after": wait}}) for _ in range(3)])
    assert telegram.send_photo(b"png") is False
    assert len(calls) == 3 and sleeps == [max(30, wait)] * 3


@pytest.mark.parametrize("raw", [PRIVATE.encode(), b"", b'[]', b'{"parameters":{"retry_after":"bad"}}'])
def test_malformed_photo_429_is_sanitized_failure(transport, caplog, raw):
    install, calls, sleeps = transport
    install([response(429, raw=raw)])
    assert telegram.send_photo(b"png") is False
    assert len(calls) == 1 and sleeps == []
    assert PRIVATE not in caplog.text


def test_long_message_retains_any_failed_chunk(transport):
    install, calls, sleeps = transport
    install([response(payload={"ok": False}), response(payload={"ok": True})])
    assert telegram.send_long_message("a" * 2000 + "\n" + "b" * 2000) is False
    assert len(calls) == 2 and sleeps == []


@pytest.mark.parametrize("payload", [{"ok": False}, {}, {"ok": True}])
def test_actual_receipt_controls_stop_update_and_trigger_record(env, transport, payload):
    symbol = "SYM-RECEIPT"
    stop_manager.add_position(symbol, entry_price=80., initial_stop=70., highest_high=100.)
    _market(env, [symbol], {symbol: ["SURGE DOWN -6%"]})
    env.monkeypatch.setattr(env.tg, "send_message", telegram.send_message)
    install, calls, sleeps = transport
    install([response(payload=payload) for _ in range(4)])
    monitor.job_stop_check([symbol])
    confirmed = payload.get("ok") is True
    state = json.loads(env.state.read_text(encoding="utf-8"))
    assert (symbol in state["alert_log"]) is confirmed
    assert stop_manager.load_all()[symbol].current_stop == (90. if confirmed else 70.)
    assert bool(monitor._problems) is not confirmed
    assert calls and sleeps == []
