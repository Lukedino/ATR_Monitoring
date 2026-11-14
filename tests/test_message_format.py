"""텔레그램 공통 양식(2026-11-14) — HTML 이스케이프·한국 시각·길이 분할·평문 재전송을 고정한다.

전송은 전부 가짜 응답이다. 예시 종목·계좌·가격은 가상 값이다.
"""
from datetime import datetime, timezone
from html.parser import HTMLParser
import importlib.util
import json
from pathlib import Path
import re
import urllib.error
import urllib.parse

import pandas as pd
import pytest
import requests

import monitor
import telegram_bot as tg
from atr_calculator import ChandelierResult
from stop_manager import UpdateResult

ALLOWED_TAGS = {"b", "i", "u", "s", "code", "pre", "a", "blockquote"}
FROZEN = datetime(2026, 11, 13, 16, 28, tzinfo=tg.KST)   # 금요일
KR = "999990.KS"


class _TagCheck(HTMLParser):
    """허용 태그만, 짝이 맞게 — 텔레그램 HTML 파서가 거절할 모양을 잡는다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.errors = [], []

    def handle_starttag(self, tag, attrs):
        if tag not in ALLOWED_TAGS:
            self.errors.append("tag:" + tag)
        self.stack.append(tag)

    def handle_endtag(self, tag):
        if not self.stack or self.stack.pop() != tag:
            self.errors.append("unbalanced:" + tag)


def assert_valid_html(text):
    checker = _TagCheck()
    checker.feed(text)
    checker.close()
    assert not checker.errors and not checker.stack, (checker.errors, checker.stack)
    # 이스케이프되지 않은 & 가 없어야 한다
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#\d+;)", text)


@pytest.fixture(autouse=True)
def frozen(monkeypatch):
    monkeypatch.setattr(tg, "_now", lambda: FROZEN)
    monkeypatch.setitem(tg.KR_STOCK_NAMES, KR, "가상전자")
    monkeypatch.setitem(tg.SYMBOL_ACCOUNTS, KR, ["가상계좌"])
    monkeypatch.delitem(tg.SYMBOL_ENTRY_PRICES, KR, raising=False)


def _ch(symbol, dist, market="US", **kw):
    close = 100.0
    return ChandelierResult(symbol=symbol, highest_high=120.0, atr=2.0, atr_pct=2.94, multiple=3.0,
                            stop_level=close * (1 - dist / 100), current_close=close,
                            dist_to_stop_pct=dist, market=market, **kw)


# ── 시각 ────────────────────────────────────────────────────────────────
def test_kst_format_converts_utc_and_names_the_weekday():
    assert tg.fmt_kst(datetime(2026, 11, 13, 7, 28, tzinfo=timezone.utc)) == "11/13(금) 16:28"
    assert tg.fmt_kst(datetime(2026, 11, 13, 7, 28)) == "11/13(금) 16:28"   # naive = UTC(GHA 서버)
    assert tg.fmt_kst() == "11/13(금) 16:28"


def test_market_day_format_adds_year_only_when_different():
    assert tg.fmt_day("2026-09-14") == "09/14(월)"
    assert tg.fmt_day("2025-12-31") == "2025/12/31(수)"
    assert tg.fmt_day("?") == "?"


def test_telegram_module_never_uses_naive_server_clock():
    source = Path(tg.__file__).read_text(encoding="utf-8")
    assert "datetime.now()" not in source


# ── 즉각 대응 ───────────────────────────────────────────────────────────
def test_trigger_alert_follows_common_format():
    text = tg.fmt_trigger_alert(KR, ["STOP NEAR 1.2% (Stop=57,400.00)", "VOLUME x2.3 (평균 대비 230%)"],
                                58100.0, 57400.0)
    lines = text.split("\n")
    assert lines[0] == "🔴 <b>[ATR] 즉각 대응 — 가상전자</b>"
    assert lines[1] == "11/13(금) 16:28 · 999990.KS · 가상계좌"
    assert "• 손절선 근접 1.2%" in lines and "• 거래량 2.3배 · 20일 평균 대비" in lines
    assert "현재가: 58,100원" in lines
    assert "손절선: 57,400원 · 거리 1.2%" in lines
    assert lines[-1].startswith("👉 ")
    for banned in ("STOP NEAR", "VOLUME", "57,400.00", "━", " | ", "x2.3"):
        assert banned not in text
    assert_valid_html(text)


@pytest.mark.parametrize("code,label", [
    ("SURGE +5.3% (Highest High 갱신)", "급등 +5.3%"),
    ("SURGE DOWN -6.1% (손실 확대 경보)", "급락 -6.1%"),
    ("GAP UP +3.2%", "갭 상승 +3.2%"),
    ("GAP DOWN -3.4% (Stop 즉시 이탈 위험)", "갭 하락 -3.4% · 손절선 이탈 위험"),
    ("STOP BREACH 1.0% 하회 (Stop=98.00) — 손절선 이탈", "손절선 이탈 · 1.0% 아래"),
    ("STOP MISMATCH 등록 Stop 이 현재가의 4.0배 (Stop=400.00) — 분할·데이터 점검 후 재등록 필요",
     "손절선 단위 불일치 · 현재가의 4.0배"),
    ("UNKNOWN <code>", "UNKNOWN <code>"),
])
def test_trigger_codes_are_translated(code, label):
    assert tg.trigger_label(code) == label


def test_variable_text_is_escaped(monkeypatch):
    monkeypatch.setitem(tg.KR_STOCK_NAMES, KR, "A&B <가상>")
    monkeypatch.setitem(tg.SYMBOL_ACCOUNTS, KR, ["<계좌>"])
    text = tg.fmt_trigger_alert(KR, ["UNKNOWN <b>raw</b>"], 100.0, None)
    assert "A&amp;B &lt;가상&gt;" in text and "&lt;계좌&gt;" in text
    assert "&lt;b&gt;raw&lt;/b&gt;" in text
    assert_valid_html(text)


def test_up_only_trigger_is_green_and_volume_only_is_yellow():
    assert tg.fmt_trigger_alert("SYN", ["GAP UP +3.2%"], 100.0, None).startswith("🟢 ")
    assert tg.fmt_trigger_alert("SYN", ["VOLUME x3.1 (평균 대비 310%)"], 100.0, None).startswith("🟡 ")


# ── 그 밖의 메시지 ─────────────────────────────────────────────────────
def test_stop_update_is_html_without_dividers():
    text = tg.fmt_stop_update(UpdateResult(symbol=KR, updated=True, prev_stop=55000.0, new_stop=57200.0,
                                           current_close=61000.0, action="UPDATED"))
    assert text.split("\n")[0] == "🟢 <b>[ATR] 손절선 상향 — 가상전자</b>"
    assert "손절선: 55,000원 → 57,200원 (+4.00%)" in text
    assert "👉 증권사 앱 지정가를 57,200원으로 갱신" in text
    assert "━" not in text and "📅" not in text
    assert_valid_html(text)


@pytest.mark.parametrize("make", [
    lambda: tg.fmt_spike_alert("SYN", 5.2, 2.86, 3.1),
    lambda: tg.fmt_stop_held("SYN", 100.0, 98.0),
    lambda: tg.fmt_breakeven_alert("SYN", 120.0),
])
def test_single_symbol_messages_share_the_header(make):
    text = make()
    assert re.match(r"^\S+ <b>\[ATR\] .+ — SYN</b>\n11/13\(금\) 16:28", text)
    assert "━" not in text and " | " not in text and "x" not in text.split("\n")[0]
    assert_valid_html(text)


def test_spike_ratio_uses_korean_multiplier():
    assert "배율: 1.82배" in tg.fmt_spike_alert("SYN", 5.2, 2.86, 3.1)


def test_weekly_reports_have_one_legend_and_no_pipes():
    summary = pd.DataFrame([{"Symbol": KR, "Close": 58100.0, "ATR": 1520.0, "ATR%": 2.63, "Spike": True},
                            {"Symbol": "SYN", "Close": 131.2, "ATR": 3.8, "ATR%": 2.9, "Spike": False}])
    report = tg.fmt_daily_report(summary, "📈 국내 ATR 주간 리포트", data_date="2026-11-12")
    assert report.split("\n")[0] == "📊 <b>[ATR] 국내 ATR 주간 리포트</b>"
    assert "데이터 기준: 11/12(목)" in report
    chandelier = tg.fmt_chandelier_report([_ch("SYN", 8.2), _ch(KR, -3.1, "KR", ema_21=98.0)])
    for text in (report, chandelier):
        assert " | " not in text and "━" not in text and "📅" not in text
        assert text.count("ℹ️") == 1
        assert_valid_html(text)
    assert "  현재가 $100.00 · 손절선 $91.80" in chandelier
    assert "  거리 8.2% · ATR% 2.9% · 배수 3.0" in chandelier
    assert "  ⚠️ 이탈 3.1% · ATR% 2.9% · 배수 3.0" in chandelier
    assert "103,100.00" not in chandelier and "103원" in chandelier.replace(",", "")


def test_daily_brief_keeps_its_shape_in_html():
    text = tg.fmt_daily_brief("KR 종가 요약", "2026-11-13", [_ch(KR, -1.0, "KR"), _ch("SYN", 20.0)])
    lines = text.split("\n")
    assert lines[0] == "📊 <b>[ATR] KR 종가 요약</b>"
    assert lines[1] == "11/13(금) 16:28 · 11/13(금) 기준 · 보유 2종목"
    assert "  • 가상전자" in lines
    assert_valid_html(text)


def test_chart_caption_is_html_and_names_the_symbol():
    assert tg.fmt_chart_caption(KR, "즉각 대응") == "📷 <b>가상전자</b> · 999990.KS · 즉각 대응"
    assert tg.fmt_chart_caption("A&B") == "📷 <b>A&amp;B</b>"


# ── 운영 알림 ───────────────────────────────────────────────────────────
def test_run_problems_translate_exception_names_and_link_the_run(monkeypatch):
    monkeypatch.setenv("GITHUB_REPOSITORY", "owner/repo")
    monkeypatch.setenv("GITHUB_RUN_ID", "42")
    monkeypatch.delenv("GITHUB_SERVER_URL", raising=False)
    text = tg.fmt_run_problems(["작업 실행 실패: ValueError",
                                "상태 파일 저장 실패 — 원격 반영 미확정: Drive state upload failed; remote result is unconfirmed",
                                "종목 처리 실패 1/2"])
    assert text.split("\n")[0] == "🔴 <b>[ATR] 실행 중 문제 3건</b>"
    assert "• 작업 실행 실패 — 원인: 값 이상 <code>ValueError</code>" in text
    assert "올렸지만 반영 확인 실패" in text
    assert '<a href="https://github.com/owner/repo/actions/runs/42">실행 로그 보기</a>' in text
    assert_valid_html(text)


def test_run_log_link_is_omitted_for_unexpected_values(monkeypatch):
    assert tg.run_log_url({"GITHUB_REPOSITORY": "owner/repo", "GITHUB_RUN_ID": "1\"><b>"}) == ""
    assert tg.run_log_url({"GITHUB_REPOSITORY": "a/b", "GITHUB_RUN_ID": "7",
                           "GITHUB_SERVER_URL": "javascript:x"}) == ""


def test_monitor_failure_message_is_html(monkeypatch):
    sent = []
    monkeypatch.setattr(monitor, "_portfolio_problem", lambda: "감시 대상 0종목 <확인>")
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    monkeypatch.setenv("GHA_JOB", "stop_check")
    monkeypatch.setattr(monitor.tg, "send_message", lambda text, *a, **k: sent.append((text, a, k)) or True)
    with pytest.raises(SystemExit):
        monitor.run_github_actions_mode()
    text, args, kwargs = sent[0]
    assert not args and "parse_mode" not in kwargs        # 기본값 = HTML
    assert text.startswith("🔴 <b>[ATR] 모니터 중단</b>\n11/13(금) 16:28")
    assert "감시 대상 0종목 &lt;확인&gt;" in text
    assert_valid_html(text)


# ── 분할 ────────────────────────────────────────────────────────────────
def test_split_prefers_section_boundaries_and_respects_utf16_limit():
    sections = ["<b>섹션 %d</b>\n" % i + "\n".join("🟢 가상 종목 줄 %d-%d" % (i, j) for j in range(40))
                for i in range(12)]
    text = "\n\n".join(sections)
    assert tg._tg_len(text) > tg.MAX_MESSAGE_LEN
    chunks = tg.split_message(text)
    assert len(chunks) > 1
    for chunk in chunks:
        assert tg._tg_len(chunk) <= tg.MAX_MESSAGE_LEN
        assert chunk.startswith("<b>섹션 ")              # 섹션 머리에서 시작한다
        assert_valid_html(chunk)
    assert "\n\n".join(chunks) == text


def test_split_falls_back_to_line_boundaries():
    text = "\n".join("<b>%04d</b> " % i + "가" * 90 for i in range(200))
    chunks = tg.split_message(text)
    assert all(tg._tg_len(c) <= tg.MAX_MESSAGE_LEN for c in chunks)
    assert "\n".join(chunks) == text
    for chunk in chunks:
        assert_valid_html(chunk)


def test_short_text_is_one_piece():
    assert tg.split_message("짧은 본문") == ["짧은 본문"]


# ── 전송: 미리보기·평문 재시도·캡션 ──────────────────────────────────────
def _response(status=200, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload if payload is not None else {"ok": True}).encode()
    return result


@pytest.fixture
def posts(monkeypatch):
    calls, outcomes = [], []
    monkeypatch.setattr(tg, "_is_configured", lambda: True)
    monkeypatch.setattr(tg.time, "sleep", lambda s: None)

    def post(*args, **kwargs):
        calls.append(kwargs)
        return outcomes.pop(0)
    monkeypatch.setattr(tg.requests, "post", post)
    return calls, outcomes


def test_message_is_html_without_link_preview(posts):
    calls, outcomes = posts
    outcomes.append(_response())
    assert tg.send_message("<b>x</b>") is True
    assert calls[0]["json"]["parse_mode"] == "HTML"
    assert calls[0]["json"]["link_preview_options"] == {"is_disabled": True}


def test_caption_is_html_and_400_retries_plain(posts):
    calls, outcomes = posts
    outcomes += [_response(400, {"ok": False}), _response()]
    assert tg.send_photo(b"png", caption="📷 <b>A&amp;B</b>") is True
    assert calls[0]["data"]["parse_mode"] == "HTML"
    assert "parse_mode" not in calls[1]["data"]
    assert calls[1]["data"]["caption"] == "📷 A&B"


def test_overlong_caption_is_sent_as_trimmed_plain_text(posts):
    calls, outcomes = posts
    outcomes.append(_response())
    assert tg.send_photo(b"png", caption="<b>" + "가" * 2000 + "</b>") is True
    assert "parse_mode" not in calls[0]["data"]
    assert len(calls[0]["data"]["caption"]) == tg.MAX_CAPTION_LEN


# ── 워크플로 실패 알림 ──────────────────────────────────────────────────
NOTICE_SOURCE = Path(__file__).resolve().parents[1] / "scripts" / "notify_workflow_failure.py"
_spec = importlib.util.spec_from_file_location("synthetic_notice_format", NOTICE_SOURCE)
notice = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(notice)
NOTICE_ENV = {"TELEGRAM_BOT_TOKEN": "123456:synthetic", "TELEGRAM_CHAT_ID": "synthetic_chat",
              "GITHUB_RUN_ID": "987", "GITHUB_REPOSITORY": "owner/repo"}


def test_workflow_notice_has_kst_time_and_run_link():
    text = notice.build_message(NOTICE_ENV, now=datetime(2026, 11, 13, 7, 28, tzinfo=timezone.utc))
    assert text.split("\n")[:2] == ["🔴 <b>[ATR] 워크플로 실패</b>", "11/13(금) 16:28"]
    assert '<a href="https://github.com/owner/repo/actions/runs/987">실행 로그 보기</a>' in text
    assert_valid_html(text)


class _Resp:
    def __init__(self, status, body=b'{"ok": true}'):
        self.status, self.body = status, body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def getcode(self):
        return self.status

    def read(self, limit):
        return self.body[:limit]


class _Opener:
    def __init__(self, outcomes):
        self.outcomes, self.fields = list(outcomes), []

    def open(self, request, *, timeout):
        self.fields.append(urllib.parse.parse_qs(request.data.decode("utf-8")))
        value = self.outcomes.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.mark.parametrize("first", [
    _Resp(400, b""),
    urllib.error.HTTPError("https://synthetic.invalid/", 400, "bad", {}, None),
])
def test_workflow_notice_400_resends_plain_once(first):
    opener = _Opener([first, _Resp(200)])
    assert notice.send_failure_notice(NOTICE_ENV, opener=opener) == "sent"
    assert len(opener.fields) == 2
    assert opener.fields[0]["parse_mode"] == ["HTML"]
    assert "parse_mode" not in opener.fields[1]
    assert "<b>" not in opener.fields[1]["text"][0]


def test_workflow_notice_second_400_fails_without_more_attempts():
    opener = _Opener([_Resp(400, b""), _Resp(400, b"")])
    assert notice.send_failure_notice(NOTICE_ENV, opener=opener) == "failed"
    assert len(opener.fields) == 2
