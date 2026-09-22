# 상태 관리와 로컬 실행 결과

2026-09-22. 이 문서는 로컬 관리 CLI, 후보 보존·복구, 입력과 Telegram 응답 확인의 현재 계약을 설명한다. 전략 배수·Stop 단계·감시 시간표는 변경하지 않는다. 실제 상태 관리 명령 실행이나 운영 전환 완료를 뜻하지 않는다.

## 관리 명령

`python position_cli.py --help` 또는 `python monitor.py --add-pos ...`의 관리 옵션을 사용한다. 관리 경로는 포트폴리오 설정·시세 수집·Telegram 모듈보다 먼저 분기하며 `.env`를 읽지 않는다. Drive 모드는 현재 프로세스 환경에 `GDRIVE_STATE_FILE_ID`와 `GOOGLE_SERVICE_ACCOUNT_JSON`을 모두 명시해야 한다. 일부 설정 누락을 로컬 모드로 대체하지 않는다. GHA에서는 관리 명령을 거절한다.

```text
python position_cli.py --list-pos
python position_cli.py --add-pos SYM-EXAMPLE 100 90
python position_cli.py --add-pos SYM-EXAMPLE 100 95 --replace
python position_cli.py --remove-pos SYM-EXAMPLE
python position_cli.py --add-pos SYM-EXAMPLE 100 90 --state-scope drive
python position_cli.py --recover-state --state-scope drive
python position_cli.py --recover-state --state-scope drive --retry
```

위 값은 가상 예시다. 등록은 수동 ENTRY/STOP을 저장하는 작업이며 포트폴리오 감시 목록에 추가하는 작업과 별개다. 신규 입력의 빈 심볼·공백/제어문자·시장 불명 숫자·비유한/비양수 가격은 거절한다. 이미 있는 항목은 `--replace`를 명시해야 재등록한다. 명시 재등록은 기존 Stop/stage를 초기화하지만 알림·완료 창·확장 필드는 보존한다. Stop이 진입가보다 낮아야 한다는 새로운 매매 규칙은 넣지 않는다.

기본 `--state-scope local`은 결과를 `local_only`로 표시한다. 목록 출력은 명시적인 로컬 관리 명령에서만 허용하며 공개 로그나 리뷰 문서로 옮기지 않는다. 관리 성공은 종료 0, 오류 및 `retry_required`는 종료 1이다.

## 후보 보존과 복구

상태 파일과 같은 경로의 OS 잠금을 전체 관리 작업에 유지한다. 비공개 `data/stop_levels.json.commands/pending.json`에는 이전 로컬·게시 기준·후보 바이트와 checksum, 단계, command id를 저장한다. 이 폴더는 Git에서 제외한다. 잠금 파일과 미해결 후보를 삭제해 우회하지 않는다.

| 관측 상태 | 처리 |
|---|---|
| 기존 로컬과 원격의 JSON 내용이 다름 | 명령을 보류하고 양쪽 정본을 그대로 유지 |
| 게시 전에 저널 저장 실패 | 원격 쓰기 0 |
| 게시 응답 유실·확인 실패 | `unknown` 후보 보존, 자동 재전송 및 새 명령 보류 |
| 원격 후보 확인 후 로컬 반영 중단 | 같은 후보를 다시 관측한 뒤 로컬 반영만 재개 |
| 복구 시 원격이 후보와 같음 | 다시 게시하지 않고 로컬 승격·완료 기록 |
| 복구 시 원격이 이전 기준과 같음 | `retry_required`; 명시 `--retry`만 동일 후보 게시 |
| 복구 시 제3의 원격 내용 또는 후보 이후 로컬 변경 | 충돌로 보류, 자동 병합·덮어쓰기 없음 |

로컬 전용 변경이 남아 있으면 일반 Drive pull/push를 차단하므로 다음 동기화가 이를 지우지 않는다. 로컬 감시의 상태 갱신은 계속 가능하다. 다만 그 갱신으로 저장 내용이 관리 후보보다 나중에 바뀌면 기존 후보의 복구도 보류한다. 이 경우 새 상태의 명시적 조정이 필요하다. 후보 게시가 목적이면 로컬 관리 직후 확인·복구하고, 중간에 감시 실행을 끼우지 않는 것이 운영 절차다.

저널에는 비공개 상태가 포함된다. 백업은 기존 비공개 상태와 같은 취급이 필요하며 공개 Git·로그·첨부에 포함하면 안 된다. 자동 삭제·보관기간 정책은 추가하지 않았다.

## 입력과 알림 결과

- CSV/XLSX는 헤더와 의미 있는 전체 행을 검증한 뒤 목록·이름·계좌·진입가 메타데이터를 함께 반영한다. 깨진 한 행을 누락하고 나머지만 성공으로 채택하지 않는다. 실제 빈 구분행과 빈 선택 진입가는 허용한다.
- `Ticker` 열이 있으면 빈 셀을 종목명으로 바꾸지 않는다. 열 자체가 없는 기존 형식의 종목 코드 입력은 유지한다. `NA`·`NULL` 문자열을 결측값으로 바꾸지 않는다. 기존 다계좌 중복·단순 평균·명시 접미사를 유지한다.
- 일반 Telegram 전송은 HTTP 200과 JSON 객체의 `ok: true`를 모두 확인해야 성공이다. 잘못된 본문을 확인된 발송으로 표시하지 않는다. 알림 확인 뒤 상태를 저장하는 기존 순서와 재시도 횟수는 유지한다.
- 로컬 one-shot은 필수 작업·계산·전송 실패를 종료 1로 전달한다. KR 실패가 같은 `--once` 실행의 US 작업을 생략시키지 않고, 앞서 성공한 상태도 되돌리지 않는다. 다음 호출에는 이전 문제를 섞지 않는다.
- 로컬 스케줄러는 기존 매일 KR/US 보고, 30분 Stop, 10분 트리거 간격을 유지한다. 개별 작업 실패는 기록하고 다음 기존 예약을 계속한다. GHA는 기존 dispatch 창·concurrency를 유지한다.

## 남은 한계

Python3.11/3.12의 Windows/Linux x86_64 설치는 `requirements.txt`에 연결된 `constraints.txt`를 사용한다. 기존 제품 직접 버전10개는 유지했고 contourpy는 Python 버전별 호환 핀을 분리했다. `requirements-dev.txt`도 같은 제약을 상속한다. 운영·합성 CI 모두 설치 후 `pip check`를 실행한다. 일반 합성 CI는 네 환경, 실제 OS 잠금은 별도 네 환경이다. constraints는 버전 제약이며 배포 artifact 해시 lock이나 GitHub Action SHA 고정과 같지 않다.

Drive의 MD5 사전 검사는 원자적 CAS가 아니다. 다른 호스트의 확인→쓰기 경쟁과 ABA 가능성은 남아 있으므로 실제 관리 시 동시 writer를 배제해야 한다. 알림 전송과 로컬/원격 저장을 하나의 트랜잭션으로 보장하지도 않는다. 응답 유실·발송 후 저장 실패에서 중복 가능성이 있으며, 새로운 자동 재전송·억제 정책을 이번 변경에 넣지 않는다. 최신성 컷오프·휴장일 정책·포트폴리오 실자료 교정·운영 관리 명령은 이 합성 검증과 구분한다.

검사는 `scripts/run_offline_tests.py`의 코드 사본과 `scripts/run_state_lock_tests.py`의 합성 자식으로 수행한다. 실제 입력·자격·Drive·Telegram을 사용하지 않는다. 설치·전체 회귀·정확한 원격 HEAD/CI 결과는 이번 변경의 최종 검증 기록에 별도로 남긴다.
