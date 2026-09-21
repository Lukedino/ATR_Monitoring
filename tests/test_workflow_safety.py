"""Exercise the real dispatch selector without a shell or production inputs."""
import os
from pathlib import Path
import re
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "atr_monitor.yml"
CI_WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"
ALLOWED_JOBS = (
    "auto", "stop_check", "crypto_stop_check", "kr_daily_report",
    "us_daily_report", "trigger_check",
)


def step(source, name):
    match = re.search(r"^      - name: " + re.escape(name) + r"\n(.*?)(?=^      - name: |\Z)",
                      source, re.M | re.S)
    assert match, f"Missing workflow step: {name}"
    return match.group(1)


def run_selector(monkeypatch, tmp_path, requested_job, event="workflow_dispatch"):
    source = step(WORKFLOW.read_text(encoding="utf-8"), "Determine job type")
    lines = []
    for line in source.split("        run: |\n", 1)[1].splitlines():
        if line.strip() and not line.startswith("          "):
            break
        lines.append(line)
    script = textwrap.dedent("\n".join(lines))
    assert "${{" not in script
    output = tmp_path / "synthetic-github-env"
    output.write_text("EXISTING=synthetic\n", encoding="utf-8")
    monkeypatch.setattr(os, "environ", {
        "REQUEST_EVENT_NAME": event,
        "REQUESTED_JOB": requested_job,
        "GITHUB_ENV": str(output),
    })
    exec(compile(script, "workflow_dispatch_selector", "exec"), {})
    return output.read_text(encoding="utf-8")


@pytest.mark.parametrize("job", ALLOWED_JOBS)
def test_dispatch_selector_accepts_only_documented_jobs(monkeypatch, tmp_path, job):
    assert run_selector(monkeypatch, tmp_path, job) == f"EXISTING=synthetic\nGHA_JOB={job}\n"


@pytest.mark.parametrize("job", [
    "", "AUTO", " stop_check", "stop_check ", "unknown_job", "stop_check\nINJECTED=1",
    "auto\r\nINJECTED=1", 'auto\"; raise Exception(\"synthetic\") #',
    "$(synthetic_command)", "${{ synthetic }}", "auto\x00",
])
def test_invalid_dispatch_input_cannot_append_environment(monkeypatch, tmp_path, job):
    with pytest.raises(SystemExit, match="^Unsupported ATR job input$"):
        run_selector(monkeypatch, tmp_path, job)
    assert (tmp_path / "synthetic-github-env").read_text(encoding="utf-8") == "EXISTING=synthetic\n"


def test_non_dispatch_event_defaults_to_auto(monkeypatch, tmp_path):
    assert run_selector(monkeypatch, tmp_path, "invalid\nINJECTED=1", "synthetic_event") == (
        "EXISTING=synthetic\nGHA_JOB=auto\n"
    )


def test_operation_keeps_single_writer_and_bounded_execution():
    source = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"^  group: atr-monitor\n  cancel-in-progress: false$", source, re.M)
    assert re.search(r"^    timeout-minutes: 30$", source, re.M)
    assert "timeout-minutes: 20" in step(source, "Run ATR Monitor")
    assert "continue-on-error" not in step(source, "Run ATR Monitor")
    fonts = step(source, "Install Korean fonts")
    assert "timeout-minutes: 2" in fonts
    assert "continue-on-error: true" in fonts
    assert source.count("continue-on-error:") == 1
    assert re.search(r"^      contents: read\b", source, re.M)
    assert "persist-credentials: false" in step(source, "Checkout repository")


def test_failure_fallback_has_only_notification_secrets_and_failure_condition():
    source = step(WORKFLOW.read_text(encoding="utf-8"), "Notify workflow failure")
    assert "if: ${{ failure() }}" in source
    assert "timeout-minutes: 2" in source
    assert "run: python scripts/notify_workflow_failure.py" in source
    assert set(re.findall(r"secrets\.([A-Z_]+)", source)) == {
        "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
    }
    assert "continue-on-error" not in source


def test_ci_is_secret_free_and_runs_guarded_tests_for_supported_python_versions():
    source = CI_WORKFLOW.read_text(encoding="utf-8")
    assert "python-version: ['3.11', '3.12']" in source
    assert "python scripts/run_offline_tests.py --self-check" in source
    assert "          python scripts/run_offline_tests.py\n" in source
    assert "python -m pip install -r requirements-dev.txt" in source
    assert "secrets." not in source
    assert "monitor.py" not in source
    assert "pull_request_target" not in source
    assert re.search(r"^  pull_request:$", source, re.M)
    assert re.search(r"^  push:\n    branches: \[main\]$", source, re.M)
    assert "group: atr-tests-${{ github.ref }}" in source
    assert "group: atr-monitor" not in source
    assert "cancel-in-progress: true" in source
    assert re.search(r"^  contents: read$", source, re.M)
    assert "persist-credentials: false" in source
