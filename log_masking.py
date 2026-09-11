"""public repo 의 GitHub Actions 로그에서 실보유 티커·종목명을 가리는 2겹 방어.

ATR_Monitoring 은 public repo 라 Actions 실행 로그를 누구나 열람할 수 있는데,
data_collector / atr_calculator / monitor 의 종목별 로그가 실보유 티커를 그대로 찍어 왔다
(2026-09-11 점검). 보유 종목 구성은 개인 자산 정보다.

2겹 구조:
  1겹 RedactingFilter — 우리 로거 레코드를 가명(SYM-xxxx)으로 치환. 호출부 84곳을 고치지 않는다.
  2겹 ::add-mask::    — 서드파티가 stdout/stderr 로 직접 찍는 티커까지 GHA 가 덮는다.
                        파이썬 로깅을 거치지 않는 경로라 1겹으로는 못 잡는다.

가명은 sha256 기반이라 실행마다 같다 — "어제 실패한 그 종목이 오늘도 실패하는지" 를 공개 로그
만으로 추적할 수 있고, 종목 정체는 로컬에서 대조해야만 드러난다.

티커는 반드시 토큰 경계(\b)에서만 매칭한다. 경계 없이 치환하면 1~2글자 티커가 참사를 일으킨다
— T(AT&T) 보유 시 "Stop이탈" 이 "SSYM-e632op이탈" 이 되어 로그 전체가 망가진다(실측된 결함).
반대로 종목명은 경계를 걸지 않는다 — 한국어는 조사가 붙어 "삼성전자의" 가 안 가려진다.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import sys
from typing import Iterable, Mapping, TextIO

# add-mask 에 등록할 최소 길이. T(AT&T)·F(Ford)·SPY 처럼 짧은 값을 GHA 에 넘기면 GHA 는 경계를
# 보지 않고 치환하므로 로그 전역의 해당 글자가 *** 가 된다. 짧은 값은 1겹만 태운다.
MIN_GHA_MASK_LEN = 4

# 티커꼴(ASCII 영숫자 + . -) 인지. 아니면 종목명으로 보고 경계를 걸지 않는다.
_TICKER_LIKE = re.compile(r"^[A-Za-z0-9.\-]+$")

_KR_SUFFIXES     = (".KS", ".KQ")
_CRYPTO_SUFFIXES = ("-USDT", "-USD")


def _digest(secret: str) -> str:
    """대소문자 무관 sha256 앞 4자리. 내장 hash() 는 PYTHONHASHSEED 때문에 실행마다 달라져 쓸 수 없다."""
    return hashlib.sha256(secret.upper().encode("utf-8")).hexdigest()[:4]


def pseudonym_for(symbol: str) -> str:
    return "SYM-" + _digest(symbol)


def build_mask_map(
    symbols: Iterable[str],
    names: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """비밀 문자열 → 가명 매핑.

    포트폴리오에 적힌 형태만 넣으면 부족하다. data_collector 는 파생 형태도 찍는다:
      :112 네이버 교차검증   → suffix 없는 6자리 코드 ("005930")
      :314 KR suffix 교정    → .KS ↔ .KQ 를 뒤집은 심볼 ("005930.KQ")
    같은 종목의 파생 형태는 모두 같은 가명을 쓴다. 종목명 가명도 해당 티커와 해시를 공유한다.
    """
    mask_map: dict[str, str] = {}
    for symbol in symbols:
        if not symbol:
            continue
        pseudonym = "SYM-" + _digest(symbol)
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
            mask_map[name] = "NAME-" + _digest(symbol)
    return mask_map


def _pattern_for(secret: str) -> str:
    """비밀 문자열이 로그에 실제로 나타나는 형태를 덮는 정규식 조각."""
    # 종목명 등 비티커는 경계를 걸지 않는다 — 한국어 조사("삼성전자의") 때문.
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
        # 긴 비밀을 먼저 치환해야 BTC-USD 가 'SYM-da85-USD' 로 반쪽 치환되지 않는다.
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
    stream: TextIO | None = None,
) -> dict[str, str]:
    """루트 로거의 모든 핸들러에 마스킹을 걸고 GHA add-mask 를 발행한다."""
    mask_map = build_mask_map(symbols, names)
    if not mask_map:
        return mask_map
    log_filter = RedactingFilter(mask_map)
    # 필터는 로거가 아니라 핸들러에 붙인다. 로거에 붙이면 data_collector 같은
    # 자식 로거가 만든 레코드에는 적용되지 않는다(logging 의 filter 전파 규칙).
    for handler in logging.getLogger().handlers:
        handler.addFilter(log_filter)
    emit_gha_masks(mask_map.keys(), stream=stream)
    return mask_map


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
    symbols = list(symbols)
    # config.KR_STOCK_NAMES 는 보유와 무관한 국내 종목명 표까지 담고 있다 → 보유분만 마스킹한다.
    held_names = {s: names[s] for s in symbols if names and s in names}
    return install(symbols, held_names, stream=stream)
