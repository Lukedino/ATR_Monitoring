> 이 문서는 Codex 등 외부 에이전트용 안내문이다. 2026-09-18 작성.

> 2026-09-22 구현 반영: ATR 계산은 H/L/C 이력과 최신 ATR의 유효성을 확인한다(Open이 없는 부분 봉은 허용). 로컬 상태는 `state_validation.py`에서 스키마를 검사하고 원자적으로 저장하며, 같은 프로세스의 갱신은 RLock으로 직렬화한다. 명시한 포트폴리오 설정이 잘못되면 예시 목록으로 전환하지 않고 실행을 중단한다. 아래 리뷰 당시 기록보다 이 안내와 `docs/input-state-hardening.md`의 현재 계약을 우선한다. 실제 입력·자격증명·운영 데이터에 대한 5절의 보호 규칙은 유지한다.

> 2026-09-20: 아래 7절의 "알림 누락 경로"·"조용한 fallback" 중 다수가 수정됐다 — 종목별 예외 격리, 전송 성공 후에만 기록, Stop 은 알림 전송 뒤 저장(`update_stop(commit=False)`), 수집률 80% 미만·포트폴리오 출처 이상은 실패 처리, `send_message` 재시도, 문제가 있던 실행은 종료코드 1. 상세는 `DEVLOG.md` 2026-09-20 절, 회귀 테스트는 `tests/test_silent_failures.py`.

# AGENTS.md — Portfolio ATR Monitor (읽기 전용 리뷰 안내)

## 1. 요약
개인 포트폴리오의 ATR 기반 Chandelier 트레일링 스톱을 계산해 손절선 상향·즉각 트리거(급등락·갭·거래량·Stop 근접)·종가 요약·주간 리포트를 텔레그램으로 보내는 Python 3.11 배치다. GitHub Actions 에서 1회 실행 후 종료하는 구조이며, 시세는 yfinance(국내 종목은 네이버 교차검증)에서 받는다. **저장소는 PUBLIC 이고 Actions 로그도 공개**이므로, 입력(포트폴리오 시트)과 상태 파일은 저장소 밖(Google Drive)에 두고 로그의 티커·종목명은 가명으로 치환한다. 리뷰 목적은 보안·오류·개선점 지적이며 코드 수정·커밋은 하지 않는다.

## 2. 디렉터리 지도
| 경로 | 역할 |
|---|---|
| `monitor.py` | 진입점. CLI·GHA 단일 실행 모드·창 실행기(`run_due_windows`)·작업 함수(`job_*`) |
| `market_hours.py` | 시장 활성 게이트(`is_market_active`) + 실행 창 정의(`ALL_WINDOWS`, `due_windows`) |
| `stop_manager.py` | 상태 파일 읽기/쓰기 — `positions`·`alert_log`(알림 중복 방지)·`done_windows`(창 멱등) |
| `drive_state.py` | 상태 파일 Drive 동기화(pull/push, 검증, md5 확인, fail-closed) |
| `log_masking.py` | 공개 로그 마스킹 2겹(로깅 필터 + GHA `::add-mask::`) |
| `config.py` | 환경변수·포트폴리오 로드(명시한 Drive 또는 `STOCK_LIST`, 무설정 로컬만 fallback)·ATR 파라미터. **import 시점에 Drive 를 호출할 수 있다** |
| `state_validation.py` | 상태 스키마·정제된 오류·원자적 로컬 쓰기·프로세스 내 공통 잠금 |
| `data_collector.py` | OHLCV 수집·최신가 동기화·심볼 교정(접미사 뒤집기, 크립토 숫자 ID 변종) |
| `atr_calculator.py` / `visualizer.py` / `telegram_bot.py` | ATR·Chandelier·트리거 계산 / 차트 PNG / 전송·메시지 포맷 |
| `dispatcher_schedules.py` → `dispatcher_schedules.json` | 창 정의에서 외부 디스패처용 슬롯(UTC cron) 목록을 생성한 산출물 |
| `.github/workflows/atr_monitor.yml` | 유일한 워크플로 |
| `tests/` | pytest 9개 파일 121건(2026-09-18 로컬 실행 기준 전부 통과) |
| `analysis/` | ATR 배수 검증용 일회성 스크립트와 집계 결과. 운영 경로 아님 |
| `DEVLOG.md`, `setup_github.sh`, `.env.example` | 개발 일지, 최초 업로드 스크립트, 환경변수 견본 |

## 3. 실행 구조
- **트리거**: `workflow_dispatch` 뿐이다(`inputs.job`: `auto`·`stop_check`·`crypto_stop_check`·`kr_daily_report`·`us_daily_report`·`trigger_check`). `schedule:` 크론은 의도적으로 제거됐고, 비공개 디스패처 저장소가 창마다 1회 `job=auto` 로 호출한다. 디스패처 측 구현은 이 저장소에서 확인 불가 → **미확인**.
- **흐름**: 워크플로가 `GHA_JOB` 환경변수를 정함 → `python monitor.py` → `GITHUB_ACTIONS=true` 면 `run_github_actions_mode()` → 상태 pull → 작업 실행 → `finally` 에서 상태 push. `concurrency: atr-monitor`(`cancel-in-progress: false`)로 실행을 직렬화한다.
- **창(window)**: `market_hours.ALL_WINDOWS` 15개. 전부 지역 시각으로 정의(KR·크립토는 Asia/Seoul, US 는 America/New_York → DST 자동). 길이 25~30분. `action` 은 `stop_check` 또는 `weekly_report`, 종가 창 2개만 `brief=True`.
- **안전망**: `job=auto` 는 창과 무관하게 **항상 전 종목 stop_check 을 1회** 돌린 뒤, 지금 열려 있는 창 중 "얹을 것이 있는 창"(종가 요약·주간 리포트)만 추가 실행한다. `stop_check` 전용 창은 실행기가 건너뛴다.
- **멱등**: 완료 표시는 상태 파일 `done_windows[창의 지역 날짜]` 에 창 이름으로 기록, 7일 보존. 실패한 창은 완료로 적지 않는다.
- **시장 활성 게이트**: 트리거 **알림 발송 여부만** 결정한다(KR 08:00~20:00 KST, US 04:00~20:00 ET, 지역 기준 주말 제외, 크립토 24/7). 데이터 수집과 Stop 갱신은 게이트와 무관하게 돈다.
- **상태 저장**: 실행 중에는 로컬 `data/stop_levels.json`(gitignore), 실행 사이에는 Drive 의 파일 1개. 파일 식별자와 자격증명은 GitHub Secrets 로만 주입된다. 서비스 계정은 새 파일을 만들 수 없어 기존 파일을 update 한다.

## 4. 로컬 실행·테스트
```bash
pip install -r requirements.txt      # 버전 고정(공급망 방어). pytest 는 목록에 없어 별도 설치
python -m pytest tests -q            # 121 passed. 네트워크·자격증명 불필요
python dispatcher_schedules.py       # 창 정의 → dispatcher_schedules.json 재생성(테스트가 일치를 강제)
python monitor.py --list-pos         # 그 밖의 CLI 옵션은 monitor.py 상단 docstring 참조
```
- 상위 경로에 `[` `]` 가 있으면 인자 없는 `pytest` 는 "path cannot contain [] parametrization" 으로 실패한다 → `tests` 를 명시한다.
- `.env` 없이 실행하면 `config.py` 의 테스트용 fallback 심볼로 돈다. 텔레그램 미설정 시 전송은 경고만 남기고 생략된다.
- CI 에서 테스트를 돌리는 워크플로는 없다(워크플로 파일은 1개뿐).

## 5. 절대 규칙
1. **보유 정보를 어디에도 쓰지 않는다** — 리뷰 결과·커밋 메시지·문서·이슈·테스트 픽스처에 실제 티커, 종목명, 계좌명, 수량, 진입가, 금액을 적지 말 것. 예시가 필요하면 `SYM-xxxx` 같은 가명이나 명백한 가상 값을 쓴다.
2. **식별자·자격증명 금지** — Drive 파일/폴더 ID, 서비스 계정 주소, 텔레그램 chat id·봇 토큰, Secret 값. Secret **이름**은 워크플로에 이미 공개돼 있으므로 언급 가능.
3. **마스킹을 우회·약화하지 않는다** — `log_masking.install_for_github_actions()` 호출 제거·지연, `print()`·`sys.stderr` 직접 출력으로 종목별 정보를 찍기, 텔레그램 메시지 원문을 로그에 남기기, 솔트 없는 해시로 되돌리기는 전부 금지 제안이다.
4. `data/stop_levels.json`, `*.xlsx`, `.env`, 서비스 계정 JSON 은 gitignore 대상이다. 추적 대상으로 바꾸는 제안을 하지 말 것.
5. 이 리뷰는 읽기 전용이다. 워크플로를 실행(dispatch)하거나 외부 API 를 호출하지 말 것.

## 6. 의도된 설계 — 결함으로 오인하지 말 것
- **`schedule:` 크론이 없다.** GHA 예약 이벤트 배달률이 매우 낮았던 실측 때문에 제거했다. `# (was) cron:` 주석은 되돌리기용이며 `tests/test_workflow_trigger.py` 가 크론 부활을 막는다.
- **매 실행마다 전 종목 stop_check 이 돈다(창 밖이어도).** 창 안에서만 체크하던 첫 설계가 손절 체크 정지를 일으켜 분리했다. 중복 실행처럼 보여도 의도다.
- **`stop_check` 전용 창이 정의만 있고 아무것도 하지 않는다.** 디스패처 슬롯 생성의 기준점으로 남겨 둔 것이다.
- **US 창의 디스패처 슬롯이 `-edt`/`-est` 두 벌이다.** 어느 날이든 하나만 창 안에 들어가고, 다른 하나는 stop_check 만 하고 끝난다(무해). 같은 UTC 분의 창은 한 슬롯으로 병합된다.
- **멱등 키는 UTC 가 아니라 창의 지역 날짜다.** UTC 를 쓰면 미국 금요일 저녁 창이 토요일로 기록된다.
- **stop_check 이 실패하면 종가 요약 창이 `RuntimeError` 를 낸다.** 완료로 기록되지 않게 해 재시도를 남기려는 것이다.
- **상태 pull 실패 시 즉시 종료(fail-closed).** 빈 상태로 돌면 전 종목 알림 스팸이 된다. GHA 에서 상태 파일 Secret 이 없을 때도 오류다.
- **로컬 실행은 마스킹하지 않는다**(`GITHUB_ACTIONS=true` 일 때만). 디버깅용이다.
- **티커는 `\b` 경계로, 종목명은 경계 없이 치환한다.** 짧은 티커가 다른 단어 속에서 치환되는 사고와, 한국어 조사가 붙은 종목명이 새는 사고를 각각 막는다.
- **4자 미만 값은 `::add-mask::` 에 등록하지 않는다**(`MIN_GHA_MASK_LEN`). GHA 마스크는 경계 없이 치환해 로그 전체를 망가뜨린다. 짧은 값은 1겹 필터만 탄다.
- **가명은 HMAC + 4자리 hex, 솔트는 `LOG_MASK_SALT` → 봇 토큰 폴백, 둘 다 없으면 `SYM-****`.** 솔트 없는 해시는 후보 대입으로 역산된다. 4자리 충돌 가능성은 감수한 선택이다.
- **`contents: read` 만 부여.** 상태를 Drive 에 두므로 저장소 쓰기 권한이 필요 없다. Drive 스코프가 상태 쪽은 `drive`, 포트폴리오 쪽은 `drive.readonly` 인 것도 의도다(update 에 쓰기 필요).
- **`requirements.txt` 버전 고정**, 텔레그램 400 폴백이 원문 대신 길이만 로그에 남기는 것, 분류 불가 심볼은 게이트가 막지 않는 것(누락보다 과다 알림 선호)도 의도다.

## 7. 리뷰 시 집중할 위험 지점
**A. 마스킹 회귀·누출 경로** — `log_masking.py`, `monitor.py:80-92`, `data_collector.py`, `config.py`
- 로깅을 거치지 않는 출력: 잡히지 않은 예외의 traceback(stderr)은 1겹을 타지 않는다. 4자 미만 티커는 2겹에도 없으므로 `KeyError` 류 메시지에 실려 나갈 수 있는지.
- 필터는 `record.getMessage()` 만 치환한다. 누군가 `logger.exception`·`exc_info=True` 를 추가하면 traceback 본문은 마스킹되지 않는다(현재 사용처 0건).
- 필터는 설치 시점의 **루트 핸들러**에만 붙는다. 이후 추가된 핸들러, `propagate=False` 로거, `config.py` import 중(설치 이전)의 로그.
- 마스크 맵에 없는 파생 문자열: 실행 중 새로 조회한 종목명(`data_collector.py:478` `enrich_kr_stock_names`, 현재 호출처 0건), 계좌명, 심볼 교정으로 생긴 새 변종.
- 예외 메시지에 URL 이 실리는 경로: `telegram_bot.py:133-138,174,192` 는 `requests` 예외를 그대로 찍는데 URL 에 봇 토큰이 들어 있다. GHA 의 Secret 자동 마스킹에만 의존하고 있는지.
- `analysis/validate_atr_multiples.py:31` 의 하드코딩 심볼 목록과 `DEVLOG.md` 가 공개에 적합한지는 소유자 판단 사항이다(리뷰에 심볼을 옮겨 적지 말 것).

**B. 워크플로** — `.github/workflows/atr_monitor.yml`
- `permissions` 최소화 유지 여부, 액션이 태그(`@v4`·`@v5`) 고정이고 SHA 고정이 아닌 점, `${{ inputs.job }}` 을 `run:` 셸에 직접 보간하는 점(choice 타입이지만 API dispatch 경로 포함해 검토).
- 낡은 주석: 45행 "push 하기 위한 쓰기 권한", 104행의 본문 없는 6단계 제목, `monitor.py:16-29,430-433` docstring 의 옛 스케줄 서술. job 에 `timeout-minutes` 미설정.

**C. 시간대·DST** — `market_hours.py`, `stop_manager.py`, `dispatcher_schedules.py`
- `stop_manager.py:73,344,392` 는 naive `datetime.now()` 를 쓴다. GHA 러너에서는 UTC 이므로 `alert_log` 의 "새 거래일" 경계가 어느 시장의 지역 날짜와도 다르다 — 창의 멱등 키와 기준이 불일치한다.
- `Window.is_due` 의 종료 시각 계산이 자정을 넘는 창에서 어떻게 되는지(현재 그런 창은 없음), DST 전환 당일의 US 슬롯, `PROBE_WEEKS` 가 2026년 날짜로 고정된 점.
- 휴장일 달력이 없다 — 게이트·창 모두 요일만 본다.

**D. 상태 파일 경쟁·부분 실패** — `stop_manager.py:80-90`, `drive_state.py`, `monitor.py:452-473`
- `_save_raw` 는 원자적 쓰기가 아니고, 모든 함수가 매번 파일 전체를 읽고 쓴다. push 는 조건부 갱신(ETag/세대 확인) 없이 마지막 쓰기가 이긴다 — 직렬화는 워크플로 `concurrency` 하나에만 의존하며 로컬 CLI(`--add-pos`)와 GHA 가 동시에 쓰면 보호 장치가 없다.
- `mark_window_done` 후 push 실패 시: 알림은 나갔는데 완료 기록이 유실 → 다음 실행에서 중복 발송. 반대로 아래 E 의 경우 미발송인데 완료로 기록된다.
- `config.py:189-210`: GHA 에서 Drive 포트폴리오 로드가 실패하면 **조용히 fallback 심볼로 계속 실행**된다(경고 로그뿐, 텔레그램 알림 없음). 상태 파일의 fail-closed 와 정책이 다르다.

**E. 알림 누락 경로** — `monitor.py`, `telegram_bot.py`
- `tg.send_message()` 는 실패 시 `False` 를 돌려주지만 호출부가 반환값을 보지 않는다. `monitor.py:163-166,323-326` 은 전송 실패여도 `mark_trigger_sent` 로 기록해 같은 조건의 재알림이 막히고, `monitor.py:364,423` 은 종가 요약 전송이 실패해도 창을 완료로 적는다.
- `monitor.py:400-404`: 전 종목 stop_check 예외는 로그만 남긴다(텔레그램 알림 없음, 종료 코드 0). 주간 리포트 창만 열려 있던 실행이라면 외부에서 실패를 알 방법이 없다.
- 종목 루프 중간의 예외 하나가 나머지 종목 체크를 전부 중단시키는지(`job_stop_check` 루프에 종목 단위 try 없음).
- `send_message` 는 429 재시도가 없고(`send_photo` 만 있음), `send_long_message` 분할 중 일부 실패를 알리지 않는다.
