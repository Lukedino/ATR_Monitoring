"""
drive_state.py — 봇 상태 파일(data/stop_levels.json)을 Google Drive 에 보관 (2026-08-30 보안점검 ④)

왜:
  이 저장소는 public 이다. 봇이 매 실행 후 stop_levels.json(alert_log = 종목별 마지막 알림 이력)을
  git 에 커밋하던 구조라 실제 보유 종목 목록이 공개 히스토리에 그대로 남았다. private 전환은
  GitHub Actions 무료 한도(private 합산 2,000분/월)를 넘겨 다른 저장소까지 멈추므로, 대신
  "코드만 public, 입력(Portfolio)과 상태(stop_levels.json)는 Drive" 구조로 바꾼다.

동작:
  - 시작: Drive → 로컬 DATA_FILE 다운로드(pull). 비어 있거나 JSON 이 아니면 StateSyncError —
    빈 기억으로 실행하면 모든 종목에 알림을 다시 보내는 스팸이 되므로 fail-closed.
  - 종료: 로컬이 바뀌었을 때만 Drive update(push). 서비스계정은 새 파일을 만들 수 없어(저장 쿼터 0,
    Pactolus/크롤러에서 검증된 제약) 사용자가 만든 placeholder 파일을 갱신한다. Drive 가 돌려준
    md5 로 업로드를 검증.
  - GitHub Actions(GITHUB_ACTIONS=true)에서 GDRIVE_STATE_FILE_ID 가 없으면 StateSyncError —
    파일이 더 이상 repo 에 없으므로 그대로 돌면 빈 기억이 된다. 로컬 개발은 미설정 시 로컬 파일만 사용.

설정:
  GitHub Secrets: GDRIVE_STATE_FILE_ID (Drive 의 stop_levels.json 파일 ID), GOOGLE_SERVICE_ACCOUNT_JSON(기존)
  Drive: 서비스계정과 공유된 폴더 안에 stop_levels.json 을 한 번 만들어 두고, 최초 1회 현재 내용을 시드
"""
from __future__ import annotations

import hashlib
import io
import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

STATE_FILE_ID_ENV = "GDRIVE_STATE_FILE_ID"
SA_JSON_ENV       = "GOOGLE_SERVICE_ACCOUNT_JSON"
REQUIRED_KEYS     = ("positions", "alert_log")


class StateSyncError(RuntimeError):
    """상태 파일 동기화 실패 — 호출자는 실행을 중단해야 한다 (빈 기억으로 돌면 알림 스팸)."""


def validate_state_bytes(raw: bytes) -> dict:
    """상태 파일 바이트가 정상인지 검사하고 dict 로 돌려준다. 순수 함수."""
    if not raw or not raw.strip():
        raise StateSyncError("Drive 상태 파일이 비어 있음 — 최초 1회 시드(현재 stop_levels.json 업로드)가 필요")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:
        raise StateSyncError(f"상태 파일 JSON 파싱 실패: {e}") from e
    if not isinstance(data, dict) or not any(k in data for k in REQUIRED_KEYS):
        raise StateSyncError("상태 파일 형식 아님 (positions/alert_log 키 없음)")
    return data


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_service(sa_json: str):
    from google.oauth2 import service_account
    from googleapiclient.discovery import build
    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive"],   # update() 에는 drive.readonly 부족
    )
    return build("drive", "v3", credentials=creds, cache_discovery=False)


class DriveState:
    def __init__(self, local_path: Path, file_id: str, service):
        self.local_path  = Path(local_path)
        self.file_id     = file_id
        self.service     = service
        self._pulled_md5: str | None = None

    def pull(self) -> dict:
        raw  = self.service.files().get_media(fileId=self.file_id).execute()
        data = validate_state_bytes(raw)              # 실패 시 로컬 파일은 건드리지 않는다
        self.local_path.parent.mkdir(parents=True, exist_ok=True)
        self.local_path.write_bytes(raw)
        self._pulled_md5 = _md5(raw)
        logger.info("Drive 상태 파일 로드 — %d bytes, alert_log %d건", len(raw), len(data.get("alert_log", {})))
        return data

    def push(self) -> bool:
        """로컬이 바뀌었으면 Drive 에 올린다. 올렸으면 True."""
        if not self.local_path.exists():
            return False
        raw = self.local_path.read_bytes()
        if _md5(raw) == self._pulled_md5:
            return False
        validate_state_bytes(raw)                     # 깨진 로컬로 Drive 의 멀쩡한 상태를 덮어쓰지 않는다
        from googleapiclient.http import MediaIoBaseUpload
        media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
        resp  = self.service.files().update(fileId=self.file_id, media_body=media,
                                            fields="id,size,md5Checksum").execute()
        if resp.get("md5Checksum") != _md5(raw):
            raise StateSyncError("Drive 업로드 후 md5 불일치 — 상태 저장 실패")
        self._pulled_md5 = _md5(raw)
        logger.info("Drive 상태 파일 갱신 — %d bytes", len(raw))
        return True


def from_env(local_path: Path) -> DriveState | None:
    """환경변수로 DriveState 를 만든다. 로컬 개발(미설정)은 None, GitHub Actions 미설정은 오류."""
    file_id = os.getenv(STATE_FILE_ID_ENV, "").strip()
    sa_json = os.getenv(SA_JSON_ENV, "")
    in_ci   = os.getenv("GITHUB_ACTIONS", "").lower() == "true"
    if not file_id or not sa_json:
        if in_ci:
            raise StateSyncError(f"GitHub Actions 에서는 {STATE_FILE_ID_ENV} 와 {SA_JSON_ENV} Secret 이 필요합니다")
        logger.warning("%s 미설정 — 로컬 파일만 사용 (Drive 동기화 없음)", STATE_FILE_ID_ENV)
        return None
    return DriveState(local_path, file_id, build_service(sa_json))
