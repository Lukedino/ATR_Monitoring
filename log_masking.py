"""public repo 의 GitHub Actions 로그에서 실보유 티커·종목명을 가리는 2겹 방어.

ATR_Monitoring 은 public repo 라 Actions 실행 로그를 누구나 열람할 수 있는데,
data_collector / atr_calculator / monitor 의 종목별 로그가 실보유 티커를 그대로 찍어 왔다
(2026-09-11 점검). 보유 종목 구성은 개인 자산 정보다.

2겹 구조:
  1겹 RedactingFilter — 우리 로거 레코드를 가명(SYM-xxxx)으로 치환. 호출부 84곳을 고치지 않는다.
  2겹 ::add-mask::    — 서드파티가 stdout/stderr 로 직접 찍는 티커까지 GHA 가 덮는다.
                        파이썬 로깅을 거치지 않는 경로라 1겹으로는 못 잡는다.
                        (GHA 가 이 명령을 로그에 에코하지 않는 것은 2026-09-11 실런에서 확인)

가명은 비밀 솔트로 HMAC 한다. 솔트 없는 sha256 은 마스킹이 아니다 — 상장 티커는 수천 개뿐이라
공격자가 전수 대입으로 표를 만들면 역산된다(2026-09-11 실측: 후보 23개만으로 SYM-ba61 에서
원본이 유일 복원됐다). 솔트가 같으면 가명은 실행마다 같아서 "어제 실패한 그 종목이 오늘도
실패하는지" 를 공개 로그만으로 추적할 수 있고, 정체는 솔트를 가진 사람만 대조할 수 있다.

티커는 반드시 토큰 경계에서만 매칭한다. 경계 없이 치환하면 1~2글자 티커가 참사를 일으킨다
— T(AT&T) 보유 시 Stop이탈 이 SSYM-xxxxop이탈 이 되어 로그 전체가 망가진다(실측된 결함).
반대로 종목명은 경계를 걸지 않는다 — 한국어는 조사가 붙어 "삼성전자의" 가 안 가려진다.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import re
import sys
from typing import Iterable, Mapping, TextIO

# add-mask 에 등록할 최소 길이. GHA 는 마스크를 토큰 경계 없이 치환하므로 T(AT&T)·F(Ford)·SPY
# 같은 짧은 값을 넘기면 로그 전역의 해당 글자가 별표가 된다. 짧은 값은 1겹만 태운다.
# GHA 자신이 이 사고를 시연한다 — 다줄 시크릿의 중괄호 한 글자를 마스크로 등록해
# shell 표기의 자리표시자를 깨뜨린다(2026-09-11 실런 로그 실측).
MIN_GHA_MASK_LEN = 4

# 솔트 조달 순서. 전용 시크릿이 없어도 동작하도록 봇 토큰을 폴백으로 쓴다
# (이 워크플로에는 항상 주입되고, GHA 가 자동 마스킹하므로 로그에 노출되지 않는다).
SALT_ENV_VARS = ("LOG_MASK_SALT", "TELEGRAM_BOT_TOKEN")

# 솔트를 못 구했을 때의 라벨. 역산 가능한 해시를 내보내는 것보다 종목 구분을 포기하는 게 안전하다.
OPAQUE_DIGEST = "****"

# 티커꼴(ASCII 영숫자 + . -) 인지. 아니면 종목명으로 보고 경계를 걸지 않는다.
_TICKER_LIKE = re.compile(r"^[A-Za-z0-9.\-]+$")

_KR_SUFFIXES     = (".KS", ".KQ")
_CRYPTO_SUFFIXES = ("-USDT", "-USD")

logger = logging.getLogger("log_masking")


def _digest(secret: str, salt: str | None) -> str:
    """비밀 솔트로 HMAC-SHA256 한 앞 4자리. 솔트가 없으면 불투명 라벨로 떨어진다."""
    if not salt:
        return OPAQUE_DIGEST
    return hmac.new(
        salt.encode("utf-8"), secret.upper().encode("utf-8"), hashlib.sha256
    ).hexdigest()[:4]


def pseudonym_for(symbol: str, salt: str | None) -> str:
    return "SYM-" + _digest(symbol, salt)


def build_mask_map(
    symbols: Iterable[str],
    names: Mapping[str, str] | None = None,
    *,
    salt: str | None,
) -> dict[str, str]:
    """비밀 문자열 → 가명 매핑.

    포트폴리오에 적힌 형태만 넣으면 부족하다. data_collector 는 파생 형태도 찍는다:
      :112 네이버 교차검증   → suffix 없는 6자리 코드 ("005930")
      :314 KR suffix 교정    → .KS 와 .KQ 를 뒤집은 심볼 ("005930.KQ")
    같은 종목의 파생 형태는 모두 같은 가명을 쓴다. 종목명 가명도 해당 티커와 해시를 공유한다.
    """
    mask_map: dict[str, str] = {}
    for symbol in symbols:
        if not symbol:
            continue
        pseudonym = "SYM-" + _digest(symbol, salt)
        mask_map[symbol] = pseudonym
        upper = symbol.upper()
        for suffix in _KR_SUFFIXES:
            if upper.endswith(suffix):
                base  = symbol[: -len(suffix)]
                other = _KR_SUFFIXES[1] if suffix == _KR_SUFFIXES[0] else _KR_SUFFIXES[0]
                mask_map.setdefault(base, pseudonym)
                mask_map.setdefault(base + other, pseudonym)
                break
        for suffix in _CRYPTO_SUFFIXES:
            if upper.endswith(suffix):
                mask_map.setdefault(symbol[: -len(suffix)], pseudonym)
                break
    for symbol, name in (names or {}).items():
        if name:
            mask_map[name] = "NAME-" + _digest(symbol, salt)
    return mask_map


def _pattern_for(secret: str) -> str:
    """비밀 문자열이 로그에 실제로 나타나는 형태를 덮는 정규식 조각."""
    # 종목명 등 비티커는 경계를 걸지 않는다 — 한국어 조사 때문.
    if not _TICKER_LIKE.match(secret):
        return re.escape(secret)
    upper = secret.upper()
    for suffix in _CRYPTO_SUFFIXES:
        if upper.endswith(suffix):
            # 야후는 신규 코인에 숫자 ID 를 붙인다 (HYPE-USD → HYPE32196-USD, data_collector:332).
            base = secret[: -len(suffix)]
            return rf"\b{re.escape(base)}\d*(?:-USDT?)?\b"
    return rf"\b{re.escape(secret)}\b"


class RedactingFilter(logging.Filter):
    """로그 레코드의 완성된 메시지에서 비밀 문자열을 가명으로 치환한다."""

    def __init__(self, mask_map: Mapping[str, str]) -> None:
        super().__init__()
        # 긴 비밀을 먼저 치환해야 BTC-USD 가 반쪽 치환되지 않는다.
        # 야후 심볼이 소문자로 내려오는 경로가 있어 IGNORECASE.
        self._rules = [
            (re.compile(_pattern_for(secret), re.IGNORECASE), pseudonym)
            for secret, pseudonym in sorted(
                ((k, v) for k, v in mask_map.items() if k), key=lambda kv: len(kv[0]), reverse=True
            )
        ]

    def filter(self, record: logging.LogRecord) -> bool:
        if not self._rules:
            return True
        # 포맷을 먼저 끝낸 뒤 치환한다. args 를 먼저 치환하면 %.4f·%d 포맷이 깨진다.
        message = masked = record.getMessage()
        for pattern, pseudonym in self._rules:
            masked = pattern.sub(pseudonym, masked)
        if masked != message:
            record.msg, record.args = masked, ()
        return True


def emit_gha_masks(values: Iterable[str], stream: TextIO | None = None) -> None:
    """GHA 워크플로 명령으로 비밀 값을 등록한다. 등록 이후의 모든 로그 출력에 적용된다."""
    out = stream if stream is not None else sys.stdout
    seen: set[str] = set()
    for value in values:
        if not value or len(value) < MIN_GHA_MASK_LEN:
            continue
        # GHA add-mask 는 대소문자를 구분한다 → 코드가 .upper() 쓰는 곳이 있어 양쪽 등록이 필요하다.
        for variant in (value, value.upper(), value.lower()):
            if variant not in seen:
                seen.add(variant)
                out.write(f"::add-mask::{variant}\n")
    out.flush()


def install(
    symbols: Iterable[str],
    names: Mapping[str, str] | None = None,
    *,
    salt: str | None,
    stream: TextIO | None = None,
) -> dict[str, str]:
    """루트 로거의 모든 핸들러에 마스킹을 걸고 GHA add-mask 를 발행한다."""
    mask_map = build_mask_map(symbols, names, salt=salt)
    if not mask_map:
        return mask_map
    log_filter = RedactingFilter(mask_map)
    # 필터는 로거가 아니라 핸들러에 붙인다. 로거에 붙이면 data_collector 같은
    # 자식 로거가 만든 레코드에는 적용되지 않는다(logging 의 filter 전파 규칙).
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
    emit_gha_masks(mask_map.keys(), stream=stream)
    return mask_map


def _resolve_salt(env: Mapping[str, str]) -> str | None:
    for name in SALT_ENV_VARS:
        value = env.get(name)
        if value:
            return value
    return None


def install_for_github_actions(
    symbols: Iterable[str],
    names: Mapping[str, str] | None = None,
    env: Mapping[str, str] | None = None,
    stream: TextIO | None = None,
) -> dict[str, str]:
    """GitHub Actions 에서만 마스킹을 건다. 로컬 실행은 디버깅을 위해 실티커를 그대로 남긴다."""
    env = os.environ if env is None else env
    if env.get("GITHUB_ACTIONS", "").lower() != "true":
        return {}
    salt = _resolve_salt(env)
    if not salt:
        logger.warning(
            "마스킹 솔트 없음(%s 미설정) — 종목 구분 없이 전부 %s 로 가린다",
            "/".join(SALT_ENV_VARS), "SYM-" + OPAQUE_DIGEST,
        )
    symbols = list(symbols)
    # config.KR_STOCK_NAMES 는 보유와 무관한 국내 종목명 표까지 담고 있다 → 보유분만 마스킹한다.
    held_names = {s: names[s] for s in symbols if names and s in names}
    return install(symbols, held_names, salt=salt, stream=stream)
