"""drive_state — 봇 상태 파일(stop_levels.json)을 public repo 커밋 대신 Google Drive 에 보관 (2026-08-30 보안점검 ④).

설계:
- 시작 시 Drive → 로컬 DATA_FILE 다운로드(pull). 비어 있거나 JSON 이 아니면 실행 중단 — 빈 기억으로 돌면
  모든 종목에 알림을 다시 보내는 스팸이 되므로 fail-closed.
- 종료 시 로컬이 바뀌었을 때만 Drive update(push). SA 는 새 파일을 만들 수 없어(저장 쿼터 0) 사용자가
  만든 placeholder 를 갱신한다. 업로드 후 Drive 가 돌려준 md5 로 검증.
- GitHub Actions 에서 GDRIVE_STATE_FILE_ID 가 없으면 실행 중단(파일이 더 이상 repo 에 없으므로).
  로컬 개발에서는 미설정 시 기존처럼 로컬 파일만 사용.
"""
import hashlib
import json
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import drive_state as ds  # noqa: E402

GOOD = json.dumps({"positions": {}, "alert_log": {"AAPL": {"date": "2026-08-30"}}}, ensure_ascii=False).encode("utf-8")


# ── 순수 검증 ────────────────────────────────────────────────
def test_validate_rejects_empty():
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(b"")
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(b"   \n")


def test_validate_rejects_invalid_json():
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(b"{not json")


def test_validate_rejects_wrong_shape():
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(b"[1, 2, 3]")
    with pytest.raises(ds.StateSyncError):
        ds.validate_state_bytes(b'{"foo": 1}')


def test_validate_accepts_state():
    d = ds.validate_state_bytes(GOOD)
    assert d["alert_log"]["AAPL"]["date"] == "2026-08-30"


# ── Drive 클라이언트 흉내 (googleapiclient 호출 형태 그대로) ──
class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self):
        return self._fn()


class _Files:
    def __init__(self, content: bytes, wrong_md5: bool = False):
        self.content = content
        self.updates = []
        self.wrong_md5 = wrong_md5

    def get_media(self, fileId):
        return _Req(lambda: self.content)

    def update(self, fileId, media_body, fields=None):
        body = media_body.getbytes(0, media_body.size())

        def _do():
            self.content = body
            self.updates.append(fileId)
            md5 = hashlib.md5(body).hexdigest()
            return {"id": fileId, "size": str(len(body)), "md5Checksum": ("0" * 32) if self.wrong_md5 else md5}
        return _Req(_do)


class _Service:
    def __init__(self, files):
        self._files = files

    def files(self):
        return self._files


def _state(tmp_path, content=GOOD, wrong_md5=False):
    files = _Files(content, wrong_md5)
    st = ds.DriveState(tmp_path / "data" / "stop_levels.json", "FILE123", service=_Service(files))
    return st, files


# ── pull ────────────────────────────────────────────────────
def test_pull_writes_local_file(tmp_path):
    st, _ = _state(tmp_path)
    st.pull()
    assert (tmp_path / "data" / "stop_levels.json").read_bytes() == GOOD


def test_pull_empty_state_raises_and_leaves_no_file(tmp_path):
    st, _ = _state(tmp_path, content=b"")
    with pytest.raises(ds.StateSyncError):
        st.pull()
    assert not (tmp_path / "data" / "stop_levels.json").exists()


# ── push ────────────────────────────────────────────────────
def test_push_skips_when_unchanged(tmp_path):
    st, files = _state(tmp_path)
    st.pull()
    assert st.push() is False
    assert files.updates == []


def test_push_uploads_when_changed(tmp_path):
    st, files = _state(tmp_path)
    st.pull()
    new = json.dumps({"positions": {}, "alert_log": {"AAPL": {"date": "2026-08-31"}}}).encode()
    (tmp_path / "data" / "stop_levels.json").write_bytes(new)
    assert st.push() is True
    assert files.updates == ["FILE123"]
    assert files.content == new


def test_push_verifies_md5(tmp_path):
    st, files = _state(tmp_path, wrong_md5=True)
    st.pull()
    (tmp_path / "data" / "stop_levels.json").write_bytes(GOOD + b"\n")
    with pytest.raises(ds.StateSyncError):
        st.push()


def test_push_refuses_invalid_local_file(tmp_path):
    """로컬 파일이 깨졌으면 Drive 의 멀쩡한 상태를 덮어쓰지 않는다."""
    st, files = _state(tmp_path)
    st.pull()
    (tmp_path / "data" / "stop_levels.json").write_bytes(b"{broken")
    with pytest.raises(ds.StateSyncError):
        st.push()
    assert files.updates == []


# ── from_env ────────────────────────────────────────────────
def test_from_env_none_when_unconfigured_locally(monkeypatch, tmp_path):
    monkeypatch.delenv("GDRIVE_STATE_FILE_ID", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    assert ds.from_env(tmp_path / "s.json") is None


def test_from_env_required_in_github_actions(monkeypatch, tmp_path):
    monkeypatch.delenv("GDRIVE_STATE_FILE_ID", raising=False)
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    with pytest.raises(ds.StateSyncError):
        ds.from_env(tmp_path / "s.json")
