"""log_masking — public repo 의 GitHub Actions 로그에서 실보유 티커·종목명을 가리는 2겹 방어.

배경: ATR_Monitoring 은 public repo 라 Actions 실행 로그를 누구나 열람할 수 있는데,
data_collector / atr_calculator / monitor 의 종목별 로그가 실보유 티커를 그대로 찍어 왔다
(2026-09-11 점검). 보유 종목 구성은 개인 자산 정보다.

2겹 구조:
  1겹 RedactingFilter — 우리 로거 레코드를 가명으로 치환. 호출부 84곳을 고치지 않는다.
  2겹 ::add-mask::    — 서드파티가 stdout/stderr 로 직접 찍는 티커까지 GHA 가 덮는다.
                        파이썬 로깅을 거치지 않는 경로라 1겹으로는 못 잡는다.
"""
import io
import logging
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import log_masking as lm  # noqa: E402

SALT = "test-salt"


@pytest.fixture
def captured_root():
    """루트 로거에 StringIO 핸들러만 달아 두고, 끝나면 원상복구한다."""
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
    root.handlers = [handler]
    root.setLevel(logging.DEBUG)
    yield stream
    root.handlers, root.level = saved_handlers, saved_level


# ── 가명 생성 ────────────────────────────────────────────────
def test_pseudonym_is_salted_hmac_not_bare_sha256():
    """가명은 비밀 솔트로 HMAC 해야 한다.

    솔트 없는 sha256 은 마스킹이 아니다 — 상장 티커는 수천 개뿐이라 공격자가 전수 대입으로
    표를 만들면 역산된다. 2026-09-11 실측: 후보 23개만으로 SYM-ba61 에서 HYPE-USD 가 유일
    복원됐다. 기대값을 박아 두어 bare sha256 으로 되돌아가면 깨지게 한다
    (그 경우 AAPL 은 SYM-1eb4 가 된다).
    """
    assert lm.pseudonym_for("AAPL", SALT) == "SYM-7a83"
    assert lm.pseudonym_for("005930.KS", SALT) == "SYM-6619"


def test_pseudonym_changes_with_salt():
    assert lm.pseudonym_for("AAPL", "salt-a") != lm.pseudonym_for("AAPL", "salt-b")


def test_pseudonym_is_stable_for_same_salt():
    """같은 솔트면 실행마다 같아야 한다 — 공개 로그만으로 재발 추적이 되는 근거."""
    assert lm.pseudonym_for("AAPL", SALT) == lm.pseudonym_for("AAPL", SALT)


def test_pseudonym_ignores_case():
    assert lm.pseudonym_for("aapl", SALT) == lm.pseudonym_for("AAPL", SALT)


def test_name_pseudonym_shares_symbol_hash():
    """종목명 가명은 같은 종목의 티커 가명과 해시를 공유해야 추적이 된다."""
    mask_map = lm.build_mask_map(["005930.KS"], {"005930.KS": "삼성전자"}, salt=SALT)
    assert mask_map["005930.KS"] == "SYM-6619"
    assert mask_map["삼성전자"] == "NAME-6619"


# ── 치환 ────────────────────────────────────────────────────
def test_longer_symbol_masked_before_its_prefix(captured_root):
    """BTC-USD 와 BTC 가 둘 다 보유일 때 BTC-USD 가 반쪽 치환되면 안 된다."""
    lm.install(["BTC-USD", "BTC"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").warning("데이터 없음: %s", "BTC-USD")
    out = captured_root.getvalue()
    assert lm.pseudonym_for("BTC-USD", SALT) in out
    assert "BTC" not in out


def test_lowercase_occurrence_is_masked(captured_root):
    """야후 심볼이 소문자로 내려오는 경로가 있어 대소문자 무관 치환이어야 한다."""
    lm.install(["AAPL"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").debug("네이버 가격 조회 실패 (%s)", "aapl")
    assert "aapl" not in captured_root.getvalue()


def test_numeric_format_is_preserved(captured_root):
    """args 를 포맷 전에 치환하면 %.4f 가 깨진다 → 포맷을 끝낸 뒤 치환해야 한다."""
    lm.install(["AAPL"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("atr_calculator").warning("%s: prev Close=%.4f 비정상값", "AAPL", 1.5)
    assert "SYM-7a83: prev Close=1.5000 비정상값" in captured_root.getvalue()


def test_korean_stock_name_is_masked(captured_root):
    """data_collector 의 종목명 조회 로그는 티커와 한글 종목명을 같이 찍는다."""
    lm.install(["005930.KS"], {"005930.KS": "삼성전자"}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").info("종목명 조회: %s → %s", "005930.KS", "삼성전자")
    out = captured_root.getvalue()
    assert "삼성전자" not in out
    assert "005930" not in out
    assert "SYM-6619" in out and "NAME-6619" in out


def test_child_logger_records_are_masked(captured_root):
    """필터를 로거에 붙이면 자식 로거 레코드에 적용되지 않는다 → 핸들러에 붙어야 한다."""
    lm.install(["AAPL"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("monitor").warning("트리거 감지 알림: %s — %s", "AAPL", ["Stop이탈"])
    assert "AAPL" not in captured_root.getvalue()


def test_unrelated_text_is_untouched(captured_root):
    lm.install(["AAPL"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("monitor").info("Stop 갱신 체크 시작 (%d종목)", 23)
    assert "Stop 갱신 체크 시작 (23종목)" in captured_root.getvalue()


# ── 경계 매칭 (2026-09-11 실서버 모사에서 잡힌 결함) ──────────
def test_single_letter_ticker_does_not_match_inside_words(captured_root):
    """T(AT&T) 보유 시 Stop이탈 의 t 가 치환돼 로그 전체가 망가지던 결함."""
    lm.install(["T"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("monitor").info("Stop 갱신 체크 시작 (%d종목)", 4)
    assert "Stop 갱신 체크 시작 (4종목)" in captured_root.getvalue()


def test_single_letter_ticker_is_still_masked_when_standalone(captured_root):
    """경계 가드가 마스킹 자체를 무력화하면 안 된다."""
    lm.install(["T"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").warning("데이터 없음: %s", "T")
    out = captured_root.getvalue()
    assert lm.pseudonym_for("T", SALT) in out
    assert "없음: T" not in out


def test_bare_kr_code_is_masked(captured_root):
    """data_collector:112 의 네이버 조회 로그는 suffix 없는 6자리 코드를 찍는다."""
    lm.install(["005930.KS"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").debug("네이버 가격 조회 실패 (%s)", "005930")
    out = captured_root.getvalue()
    assert "005930" not in out
    assert "SYM-6619" in out


def test_korean_name_with_particle_is_masked(captured_root):
    """한국어는 조사가 붙는다 → 종목명에 토큰 경계를 걸면 삼성전자의 가 안 가려진다."""
    lm.install(["005930.KS"], {"005930.KS": "삼성전자"}, salt=SALT, stream=io.StringIO())
    logging.getLogger("monitor").info("%s 데이터 지연", "삼성전자의")
    assert "삼성전자" not in captured_root.getvalue()


# ── 파생 심볼 (포트폴리오에 적힌 문자열과 다른 형태) ──────────
def test_flipped_kr_suffix_is_masked(captured_root):
    """data_collector:314 는 .KS 와 .KQ 를 뒤집은 심볼을 찍는다 (suffix 자동 교정)."""
    lm.install(["005930.KS"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").warning(
        "KR suffix 자동 교정: %s → %s 로 데이터 수신", "005930.KS", "005930.KQ"
    )
    assert "005930" not in captured_root.getvalue()


def test_yahoo_numeric_id_crypto_variant_is_masked(captured_root):
    """data_collector:332 는 야후가 숫자 ID 를 붙인 변종을 찍는다 (HYPE-USD → HYPE32196-USD)."""
    lm.install(["HYPE-USD"], {}, salt=SALT, stream=io.StringIO())
    logging.getLogger("data_collector").warning(
        "크립토 심볼 자동 교정: %s → %s", "HYPE-USD", "HYPE32196-USD"
    )
    assert "HYPE" not in captured_root.getvalue()


# ── GHA add-mask ────────────────────────────────────────────
def test_short_symbols_are_excluded_from_gha_masks():
    """짧은 값을 add-mask 에 넣으면 GHA 가 경계를 안 보고 치환해 로그가 망가진다.

    GHA 자신이 이 사고를 시연한다 — 다줄 시크릿의 중괄호 한 글자를 마스크로 등록해
    shell 표기 bash -e 뒤의 자리표시자가 별표로 깨져 찍힌다 (2026-09-11 실런 로그 실측).
    """
    stream = io.StringIO()
    lm.emit_gha_masks(["T", "SPY", "AAPL"], stream=stream)
    emitted = stream.getvalue()
    assert "::add-mask::AAPL" in emitted
    assert "::add-mask::T\n" not in emitted
    assert "::add-mask::SPY" not in emitted


def test_gha_masks_cover_case_variants():
    """GHA add-mask 는 대소문자를 구분한다 → 코드가 .upper() 쓰는 곳이 있어 양쪽 등록이 필요하다."""
    stream = io.StringIO()
    lm.emit_gha_masks(["BTC-USD"], stream=stream)
    emitted = stream.getvalue()
    assert "::add-mask::BTC-USD" in emitted
    assert "::add-mask::btc-usd" in emitted


# ── GHA 게이트 + 솔트 조달 ──────────────────────────────────
def test_local_run_keeps_real_tickers(captured_root):
    """로컬 실행은 디버깅을 위해 실티커를 그대로 남긴다."""
    assert lm.install_for_github_actions(["AAPL"], {}, env={}, stream=io.StringIO()) == {}
    logging.getLogger("data_collector").warning("데이터 없음: %s", "AAPL")
    assert "AAPL" in captured_root.getvalue()


def test_github_actions_run_masks_tickers(captured_root):
    lm.install_for_github_actions(
        ["AAPL"], {},
        env={"GITHUB_ACTIONS": "true", "LOG_MASK_SALT": SALT},
        stream=io.StringIO(),
    )
    logging.getLogger("data_collector").warning("데이터 없음: %s", "AAPL")
    assert "AAPL" not in captured_root.getvalue()


def test_only_held_symbols_get_name_masking():
    """config.KR_STOCK_NAMES 는 보유와 무관한 국내 종목명 표까지 담고 있다."""
    names = {"005930.KS": "삼성전자", "000660.KS": "SK하이닉스"}
    mask_map = lm.install_for_github_actions(
        ["005930.KS"], names,
        env={"GITHUB_ACTIONS": "true", "LOG_MASK_SALT": SALT},
        stream=io.StringIO(),
    )
    assert "삼성전자" in mask_map
    assert "SK하이닉스" not in mask_map


def test_telegram_token_is_used_as_salt_fallback():
    """전용 시크릿을 따로 안 만들어도 동작해야 한다 — 이 워크플로에는 봇 토큰이 항상 있다."""
    mask_map = lm.install_for_github_actions(
        ["AAPL"], {},
        env={"GITHUB_ACTIONS": "true", "TELEGRAM_BOT_TOKEN": "bot-token-value"},
        stream=io.StringIO(),
    )
    assert mask_map["AAPL"] == lm.pseudonym_for("AAPL", "bot-token-value")
    assert mask_map["AAPL"] != "SYM-1eb4"  # bare sha256 이 아님


def test_dedicated_salt_secret_takes_precedence():
    mask_map = lm.install_for_github_actions(
        ["AAPL"], {},
        env={"GITHUB_ACTIONS": "true", "LOG_MASK_SALT": SALT,
             "TELEGRAM_BOT_TOKEN": "bot-token-value"},
        stream=io.StringIO(),
    )
    assert mask_map["AAPL"] == lm.pseudonym_for("AAPL", SALT)


def test_without_any_salt_pseudonyms_are_opaque():
    """솔트를 못 구하면 역산 가능한 해시를 내보내는 대신 전부 불투명 라벨로 떨어져야 한다."""
    mask_map = lm.install_for_github_actions(
        ["AAPL", "005930.KS"], {}, env={"GITHUB_ACTIONS": "true"}, stream=io.StringIO()
    )
    assert set(mask_map.values()) == {"SYM-****"}
