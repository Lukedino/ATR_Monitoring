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
    md5 로 업로드를 검증. 변경분 업로드 전 원격 md5 와 검증한 pull 기준값을 비교한다.
    이 사전 검사는 원자적 조건부 갱신이 아니므로 검사와 쓰기 사이의 경쟁까지 막지는 않는다.
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
import re

from state_lock import StateLockError
from state_validation import (
    StateValidationError, atomic_write_state_bytes, state_locked,
    validate_state_bytes as _validate_state_bytes,
)

logger = logging.getLogger(__name__)

STATE_FILE_ID_ENV = "GDRIVE_STATE_FILE_ID"
SA_JSON_ENV       = "GOOGLE_SERVICE_ACCOUNT_JSON"
REQUIRED_KEYS     = ("positions", "alert_log")


class StateSyncError(RuntimeError):
    """상태 파일 동기화 실패 — 호출자는 실행을 중단해야 한다 (빈 기억으로 돌면 알림 스팸)."""


def validate_state_bytes(raw: bytes) -> dict:
    """상태 파일 바이트가 정상인지 검사하고 dict 로 돌려준다. 순수 함수."""
    try:
        return _validate_state_bytes(raw)
    except StateValidationError as error:
        raise StateSyncError(str(error)) from None


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def build_service(sa_json: str):
    # Google/HTTP exceptions can embed credentials, URLs and file identifiers.
    # Keep the stage, never the original exception text or traceback chain.
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except Exception:
        raise StateSyncError("Drive client dependency initialization failed") from None
    try:
        creds = service_account.Credentials.from_service_account_info(
            json.loads(sa_json), scopes=["https://www.googleapis.com/auth/drive"],
        )
    except Exception:
        raise StateSyncError("Drive authentication configuration failed") from None
    try:
        return build("drive", "v3", credentials=creds, cache_discovery=False)
    except Exception:
        raise StateSyncError("Drive service initialization failed") from None


class DriveState:
    def __init__(self, local_path: Path, file_id: str, service):
        self.local_path  = Path(local_path)
        self.file_id     = file_id
        self.service     = service
        self._pulled_md5: str | None = None

    def pull(self) -> dict:
        try:
            return self._pull_locked()
        except StateLockError:
            self._pulled_md5 = None
            raise StateSyncError("Drive state download failed at the local state lock") from None

    @state_locked(lambda self: self.local_path)
    def _pull_locked(self) -> dict:
        self._guard_pending()
        raw = self.observe()
        data = validate_state_bytes(raw)
        try:
            atomic_write_state_bytes(self.local_path, raw)
        except StateValidationError as error:
            self._pulled_md5 = None
            raise StateSyncError(str(error)) from None
        logger.info("Drive 상태 파일 로드 — %d bytes, alert_log %d건", len(raw), len(data.get("alert_log", {})))
        return data

    def _guard_pending(self):
        # 미게시 관리 후보를 일반 pull/push가 지우거나 우회하지 못하게 한다.
        from position_commands import PositionCommandError, assert_no_pending
        try:
            assert_no_pending(self.local_path, remote=True)
        except PositionCommandError:
            self._pulled_md5 = None
            raise StateSyncError("State management recovery is required before Drive synchronization") from None

    @state_locked(lambda self: self.local_path)
    def observe(self) -> bytes:
        """로컬 정본을 건드리지 않고 원격 바이트와 게시 기준 MD5를 취득한다.

        관리 복구 전용 경계이며 일반 실행은 pending 보호가 있는 pull을 쓴다.
        """
        # A failed refresh must not leave an older download authorizing writes.
        self._pulled_md5 = None
        try:
            raw = self.service.files().get_media(fileId=self.file_id).execute()
        except Exception:
            raise StateSyncError("Drive state download failed") from None
        validate_state_bytes(raw)
        self._pulled_md5 = _md5(raw)
        return raw

    def push(self) -> bool:
        """검증한 pull 이후 변경분만 사전 충돌 검사 후 올린다. 올렸으면 True.

        The metadata check detects changes already visible before the upload;
        it is not an atomic compare-and-swap. A concurrent writer between this
        check and update, or content changed and restored (ABA), can be missed.
        """
        try:
            return self._push_locked()
        except StateLockError:
            self._pulled_md5 = None
            raise StateSyncError("Drive state upload failed at the local state lock") from None

    @state_locked(lambda self: self.local_path)
    def _push_locked(self) -> bool:
        self._guard_pending()
        try:
            if not self.local_path.exists():
                return False
            raw = self.local_path.read_bytes()
        except OSError:
            raise StateSyncError("Local state file could not be read") from None
        return self.publish(raw)

    @state_locked(lambda self, raw: self.local_path)
    def publish(self, raw: bytes) -> bool:
        """동일 client가 observe한 기준에 검증된 후보 1개를 게시한다.

        내구 후보/복구 판단은 position_commands가 담당하며 자동 재시도는 없다.
        """
        local_md5 = _md5(raw)
        if local_md5 == self._pulled_md5:
            return False
        validate_state_bytes(raw)                     # 깨진 로컬로 Drive 의 멀쩡한 상태를 덮어쓰지 않는다
        if self._pulled_md5 is None:
            raise StateSyncError("Drive state upload requires a successful pull")
        try:
            from googleapiclient.http import MediaIoBaseUpload
            media = MediaIoBaseUpload(io.BytesIO(raw), mimetype="application/json", resumable=False)
        except Exception:
            raise StateSyncError("Drive upload preparation failed") from None
        try:
            remote = self.service.files().get(
                fileId=self.file_id, fields="md5Checksum",
            ).execute()
        except Exception:
            raise StateSyncError("Drive state preflight check failed; upload was not attempted") from None
        remote_md5 = remote.get("md5Checksum") if isinstance(remote, dict) else None
        if not isinstance(remote_md5, str) or re.fullmatch(r"[0-9a-fA-F]{32}", remote_md5) is None:
            raise StateSyncError("Drive state preflight checksum is invalid; upload was not attempted")
        if remote_md5.lower() != self._pulled_md5:
            raise StateSyncError("Drive state changed remotely; upload was not attempted")
        try:
            # One attempt only: a timeout may occur after the server committed.
            # An uncertain write revokes the baseline until a new pull succeeds.
            resp = self.service.files().update(fileId=self.file_id, media_body=media,
                                               fields="id,size,md5Checksum").execute()
        except Exception:
            self._pulled_md5 = None
            raise StateSyncError("Drive state upload failed; remote result is unconfirmed") from None
        if not isinstance(resp, dict) or resp.get("md5Checksum") != local_md5:
            self._pulled_md5 = None
            raise StateSyncError("Drive upload verification failed; remote result is unconfirmed")
        self._pulled_md5 = local_md5
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
    try:
        return DriveState(local_path, file_id, build_service(sa_json))
    except StateSyncError:
        raise
    except Exception:
        raise StateSyncError("Drive state initialization failed") from None
