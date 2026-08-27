# Portfolio ATR Monitor — 개발 로그

## 프로젝트 개요

포트폴리오 종목의 ATR(Average True Range) 기반 Trailing Stop을 자동 모니터링하고
텔레그램 알림을 전송하는 시스템.

- **레포**: https://github.com/Lukedino/ATR_Monitoring (public, main 브랜치)
- **운영 방식**: GitHub Actions 완전 자동 운영 (서버 불필요)
- **기술 스택**: yfinance + pandas + matplotlib + Telegram Bot API (requests)
- **포트폴리오 소스**: Google Drive (Portfolio.xlsx) 또는 STOCK_LIST 환경변수 fallback

---

## 파일 구조

```
Portfolio_ATR_Monitor/
├── monitor.py          # 메인 진입점 — 스케줄러 / GHA 실행 모드
├── config.py           # 설정 / 포트폴리오 로드 / ATR 파라미터
├── data_collector.py   # yfinance OHLCV 데이터 수집
├── atr_calculator.py   # ATR 계산 / Chandelier Stop / 트리거 감지
├── stop_manager.py     # Stop 레벨 영속성 (data/stop_levels.json)
├── telegram_bot.py     # 텔레그램 전송 + 메시지 포매터
├── visualizer.py       # matplotlib 차트 생성 (PNG bytes)
├── requirements.txt
├── .env.example
└── .github/workflows/atr_monitor.yml
```

---

## 시스템 흐름

```
GitHub Actions (cron)
    │
    ▼
monitor.py
    │
    ├─ fetch_portfolio()     ← yfinance (OHLCV)
    ├─ calc_chandelier_stop()  ← ATR × multiple → Stop 계산
    ├─ update_stop()         ← stop_levels.json 갱신 (상향만)
    ├─ check_immediate_triggers() ← 즉각 대응 신호 감지
    └─ telegram_bot.send_*() ← 알림 전송
```

---

## 스케줄 (KST 기준)

| 시각 | 요일 | 작업 | 내용 |
|---|---|---|---|
| 00:00 ~ 23:30 (30분 간격) | **평일** | **stop_check** | 전 종목 Chandelier Stop 갱신 + 트리거 감지 |
| 00:00 ~ 23:30 (30분 간격) | **주말** | **crypto_stop_check** | 크립토 전용 (KR/US 미장) |
| **17:00** | 평일 | **kr_daily_report** | 국내 종목 ATR 일일 리포트 |
| **09:00** | 매일 | **us_daily_report** | 미국+크립토 ATR 일일 리포트 |
| 수동 | - | 위 작업 전체 | Actions 탭 → Run workflow |

> **stop_check / crypto_stop_check** 는 이벤트 없으면 무음. daily_report 는 무조건 전송.

### 시장별 모니터링 시간대 (KST)

| 시장 | 운영 시간 | 모니터링 |
|---|---|---|
| 국내 (KR) | 09:00~15:30 (공식) → **08:00~17:00** 확장 | 평일 stop_check |
| 미국 Pre-market | **18:00~22:30** (EST 04:00~09:30) | 평일 stop_check |
| 미국 Regular | **22:30~05:00** (EST 09:30~16:00) | 평일 stop_check |
| 미국 After-hours | **05:00~10:00** (EST 16:00~20:00) | 평일 stop_check |
| 크립토 | **24시간 365일** | 평일 stop_check + 주말 crypto_stop_check |

---

## 텔레그램 알림 종류

### stop_check 실행 시 (조건부 발송)

| 알림 | 발송 조건 |
|---|---|
| 🔺 ATR Trailing Stop 갱신 | 등록 포지션의 Stop이 상향됐을 때 |
| 🚨 즉각 대응 트리거 감지 | 급등/급락/갭/Stop근접 신호 감지 시 |

### kr_daily_report 실행 시 (무조건 발송, 평일 KST 17:00)

1. 📈 국내 포트폴리오 ATR 일일 리포트 (국내 종목 요약)
2. ATR% 바차트 이미지 (국내 종목)
3. 📊 ATR Trailing Stop 현황 (국내 종목, Stop거리 오름차순)
4. 종목별 미니 차트 × N장 (Stop근접+스파이크)

### us_daily_report 실행 시 (무조건 발송, 매일 KST 09:00)

1. 🌎 미국/크립토 포트폴리오 ATR 일일 리포트 (미국+크립토 요약)
2. ATR% 바차트 이미지 (미국+크립토)
3. 📊 ATR Trailing Stop 현황 (미국+크립토, Stop거리 오름차순)
4. 종목별 미니 차트 × N장 (Stop근접+스파이크)

### 메시지 포맷 예시

```
⚠️ ATR 스파이크 알림
━━━━━━━━━━━━━━━━
*삼성전자 | 005930.KS*
대상: ISA계좌, 연금저축
ATR 3,200원 | 평균 2,100원 | 배율 1.52x | ATR% 4.41%
━━━━━━━━━━━━━━━━
📅 2026-03-02 09:30
```

```
🔺 ATR Trailing Stop 갱신
━━━━━━━━━━━━━━━━
*AAPL*
대상: 해외증권
현재가 $195.43
━━━━━━━━━━━━━━━━
$178.20  →  $182.50  (+2.41% ↑)
━━━━━━━━━━━━━━━━
⚠️ 증권사 앱 지정가 갱신 필요!
📅 2026-03-02 10:00
```

```
📊 ATR Trailing Stop 현황
━━━━━━━━━━━━━━━━
📅 2026-03-02 15:35
모니터링 15종목 | Stop근접 2개
━━━━━━━━━━━━━━━━

🔴 *NVDA* [해외증권] (US·x2.0)
   현재가 $875.40 | Stop $820.00 | Gap 6.3% | ATR% 3.21%

🟢 *삼성전자 | 005930.KS* [ISA계좌] (KR·x2.0)
   현재가 72,500원 | Stop 67,000원 | Gap 7.6% | ATR% 3.41%
```

---

## 핵심 설계 — ATR Chandelier Stop

### 시장별 ATR 배수 (기본값)

| 시장 | 배수 | 근거 |
|---|---|---|
| KR (한국) | 2.0× | ATR% 중앙값 3.41%, 1.5×는 FSR 39.5%로 과도 |
| US (미국) | 2.0× | ATR% 중앙값 2.32% |
| Crypto | 2.5× | ATR% 중앙값 4.94%, 높은 변동성 반영 |
| ETF | 2.0× | US 주식 수준 |

### ATR% 구간별 배수 보정

| ATR% 구간 | 배수 조정 | 예시 (US 기준) |
|---|---|---|
| ≥ 10% | -0.5 | 2.0 → 1.5× |
| ≥ 7% | -0.25 | 2.0 → 1.75× |
| < 7% | 0 | 2.0× 유지 |

### Stop 갱신 원칙

- **상향만 허용** (하향 절대 불가 — Trailing 원칙)
- `new_stop > current_stop` 일 때만 갱신 + 텔레그램 알림
- `data/stop_levels.json` 에 영속 저장, GHA가 자동 commit/push

---

## 포트폴리오 로드 우선순위

```
1순위: Google Drive (Service Account + 파일 ID)
    → Portfolio.xlsx (MIME 자동 감지: Sheets/xlsx/csv 모두 지원)

2순위: GitHub Secret STOCK_LIST (JSON 문자열, 레거시)
    → {"국내주식": ["005930.KS"], "해외주식": ["AAPL"]}

3순위: 로컬 fallback (코드 내 _PORTFOLIO_FALLBACK)
    → 삼성전자, AAPL, NVDA, BTC-USD, SPY
```

### Portfolio.xlsx 헤더 형식

| 계좌 | 구분 | 종목 | Ticker | 거래일 | 진입가격 |
|---|---|---|---|---|---|
| ISA계좌 | 한국 | 삼성전자 | 005930 | 2024-01-15 | 72000 |
| 해외증권 | 미국 | Apple | AAPL | 2024-02-01 | 185.00 |
| 해외증권 | 크립토 | Bitcoin | BTC | 2024-03-01 | 65000 |

- **Ticker 컬럼** (우선) = yfinance 종목코드 (suffix 없이: `005930`, `AAPL`, `BTC`)
- **종목 컬럼** = 표시용 한글/영문 종목명 (알림 메시지에 `종목명 | 코드` 형식으로 표시)
- **구분 컬럼** → yfinance suffix 자동 적용:
  - 한국 → `.KS` / 코스닥 → `.KQ` / 크립토 → `-USD` / 미국·ETF → 없음
- Ticker 컬럼 없으면 종목 컬럼을 코드로 사용 (레거시 호환)
- 같은 종목이 여러 계좌에 있으면 ATR은 한 번만 계산, 알림에 모든 계좌 표시

---

## GitHub Actions 설정

### 필요한 Secrets (레포 Settings → Secrets)

| Secret 이름 | 필수 | 설명 |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | BotFather에서 발급 |
| `TELEGRAM_CHAT_ID` | ✅ | 봇과 대화 후 `/getUpdates`로 확인 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | 권장 | GCP Service Account JSON 키 전체 |
| `GDRIVE_PORTFOLIO_FILE_ID` | 권장 | Drive URL의 `/d/{ID}/` 부분 |
| `STOCK_LIST` | fallback | `{"국내주식":["005930.KS",...]}` |
| `ACCOUNT_SIZE` | 선택 | 계좌 크기 (원, 기본 10,000,000) |
| `RISK_PER_TRADE` | 선택 | 거래당 리스크 비율 (기본 0.01 = 1%) |

### GCP / Drive 설정 (Mr.Stock-Market-Crawler와 공유)

- **GCP 프로젝트**: (공개 저장소라 기재하지 않음 — GitHub Secrets 참조)
- **Service Account**: (`GOOGLE_SERVICE_ACCOUNT_JSON` Secret 의 client_email)
- **Drive 루트 폴더**: (`GDRIVE_FOLDER_ID` Secret)
- Portfolio.xlsx 위치: Drive `input/` 폴더

---

## 포지션 Stop 수동 등록 (선택 기능)

Chandelier 리포트는 포지션 등록 없이도 전 종목 표시됨.
등록 시 → Stop 갱신 알림 + Breakeven 전환 기능 활성화.

```bash
# 포지션 등록
python monitor.py --add-pos 005930.KS 72000 67000
#                             종목코드  진입가  초기Stop

# 포지션 제거
python monitor.py --remove-pos 005930.KS

# 등록 현황 조회
python monitor.py --list-pos
```

---

## 주요 변경 이력

| 날짜 | 커밋 | 내용 |
|---|---|---|
| 2026-03-02 | `8eab456` | Drive 연동 강화 (xlsx MIME 버그 수정) + position_sizer 분리 |
| 2026-03-02 | `d242da6` | 포트폴리오 중복 심볼 제거 + 리포트 정보 보강 (ATR절대값, ATR%) |
| 2026-03-02 | `1b0c54b` | 포트폴리오 계좌 정보 수집 및 알람 메시지 표시 (대상: XX계좌) |
| 2026-03-02 | `8cb1b42` | 텔레그램 대용량 메시지 분할 전송 / 차트 필터링 (Stop근접+ATR스파이크만) / sleep(2) 추가 |
| 2026-03-03 | `35dbf5c` | 텔레그램 400/429 오류 근본 수정: MAX 4000→2500(이모지 UTF-16), 3회 재시도 + 최소 30초 대기, sleep(4) |
| 2026-03-03 | `1178e1c` | GHA daily_report 스케줄 감지 수정: 정확한 분(minute) 비교 → 시간(hour) 범위 비교 |
| 2026-03-04 | `2ebde26` | 스케줄 전면 재편: 시장별 24h 모니터링 + kr/us daily_report 분리 |
| 2026-03-05 | *(이번)* | 진입가격 ATR 분석 추가: Drive 포트폴리오에서 진입가 수집, 리포트에 수익률·R배수·관리 힌트 표시 |

### 진입가격 기반 ATR 분석 (2026-03-05 추가)

Portfolio.xlsx의 `진입가격` 컬럼을 활용해 **현재가 기준** ATR 분석에 **진입가 기준** 분석을 병행 표시.

#### R배수 (ATR 단위 수익/손실)

```
R배수 = (현재가 - 진입가) / ATR
```

- `+2.3ATR` → 현재 수익이 현재 ATR의 2.3배 → **적극적 Stop 관리** (≥ 2ATR)
- `+1.1ATR` → 수익이 1ATR 이상 → **Breakeven Stop 고려** (≥ 1ATR)
- `-1.6ATR` → 손실 포지션 → **⚠️ 손실 포지션**

#### 일일 리포트 / Chandelier Stop 현황 표시 예시

```
🟢 *AAPL* [해외증권] (US·x2.0)
   현재가 $195.43 | Stop $180.00 | Gap 7.9% | ATR% 2.32%
   진입 $185.00 | +5.6% (+2.3ATR) → Breakeven Stop 고려
```

#### 구현 세부

- `config.py`: `_parse_portfolio_df()`에서 `진입가격` 컬럼 수집 → `SYMBOL_ENTRY_PRICES` 전역 export
  - 동일 종목이 복수 계좌에 있으면 진입가 평균 사용
- `telegram_bot.py`: `_entry_info_line(symbol, close, atr)` 헬퍼 → `fmt_daily_report` / `fmt_chandelier_report` 양쪽에 적용
  - 진입가 미등록 종목은 기존 포맷 그대로 (비침습적)

---

### 텔레그램 전송 오류 원인 및 해결

#### 400 Bad Request (메시지 파싱 실패)
- **원인**: Telegram은 메시지 길이를 UTF-16 code unit 기준 계산. 이모지(🟢, 📈 등)는 SMP 문자로 surrogate pair = 2 code units. 110종목 × 1이모지 ≈ 110 초과 → MAX=4000 Python chars가 4096 Telegram limit 초과
- **해결**: `send_long_message` MAX 4000 → **2500** (이모지 오버헤드 여유 확보)

#### 429 Too Many Requests (Rate Limit)
- **원인 1**: `time.sleep(2)` → 분당 30장, Telegram 봇 한도(분당 ~20장) 초과
- **원인 2**: 이전 retry의 `retry_after` 파싱값이 1~2초로 극소 → 재시도 직후 또 429
- **해결**: `time.sleep(4)` (분당 15장), retry 시 `max(retry_after, 30)` 최소 30초 보장, 3회 루프

#### GHA daily_report 미실행
- **원인**: GHA cron은 지정 시간 대비 10~30분 지연 실행 빈번. 정확한 분(minute) 비교 실패 시 stop_check로 오분류
- **해결**: `MINUTE=35` 정확 비교 → `HOUR=06 AND MINUTE≥30` 범위 비교, `HOUR=13` (stop_check cron 없는 시간대)은 전체 허용

---

## 로컬 실행 (개발/테스트)

```bash
# 의존성 설치
pip install -r requirements.txt

# .env 파일 작성
cp .env.example .env
# → TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 입력

# 1회 전체 리포트
python monitor.py --once

# Stop 갱신 체크만
python monitor.py --stop-check

# 특정 종목 차트 전송
python monitor.py --chart AAPL

# 스케줄러 데몬 (로컬)
python monitor.py
```
