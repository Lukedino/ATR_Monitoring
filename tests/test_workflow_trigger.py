"""워크플로 트리거 불변식 — 예약 실행은 Personal Assistant 디스패처가 맡는다(2026-09-17 이관).

GitHub `schedule:` 크론은 배달률이 7% 라(2026-09-15 실측) 쓰지 않는다. 대신 PA 의 tick 디스패처가
창마다 1회 `workflow_dispatch(job=auto)` 를 보낸다(슬롯은 dispatcher_schedules.py 가 생성한다).
이 테스트는 누가 크론을 되살리거나(디스패치와 겹쳐 이중 실행), `auto` 선택지·폴백을 지우거나
(디스패치가 422 로 거절되거나 창 판정을 못 탐) 하는 조용한 회귀를 막는다.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(ROOT, ".github", "workflows", "atr_monitor.yml")


def _src() -> str:
    with open(WORKFLOW, encoding="utf-8") as f:
        return f.read()


def test_no_schedule_trigger():
    """`on:` 아래 `schedule:` 키가 없어야 한다 — 되살리면 디스패치와 이중 실행된다."""
    assert re.search(r"^\s+schedule:\s*$", _src(), re.M) is None


def test_old_cron_is_kept_as_a_revert_note():
    assert "# (was) cron: '3,13,23,33,43,53 0,6,8-10,12-15,18-21,23 * * *'" in _src()


def test_workflow_dispatch_job_input_offers_auto():
    """PA 항목이 inputs.job=auto 를 보낸다 — 선택지에서 빠지면 GitHub 이 422 로 거절한다."""
    assert re.search(r"options:\s*\r?\n\s*- auto\b", _src())


def test_dispatched_runs_use_inputs_job_and_everything_else_falls_back_to_auto():
    src = _src()
    assert "REQUESTED_JOB: ${{ inputs.job }}" in src
    assert 'os.environ.get("REQUESTED_JOB", "")' in src
    assert 'else "auto"' in src
    assert 'output.write("GHA_JOB=" + job + "\\n")' in src
    assert "github.event.schedule" not in src
