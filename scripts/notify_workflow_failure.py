"""Dependency-independent fallback notification for an ordinary workflow failure.

This can duplicate an application failure message. It does not monitor absent
workflow runs or guarantee delivery after job cancellation/timeouts. A network
timeout is ambiguous, so this helper does not retry or claim exactly-once send.
"""
from datetime import datetime, timedelta, timezone
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10
KST = timezone(timedelta(hours=9))   # 한국은 서머타임이 없다 — tzdata 없이 고정 오프셋
_WEEKDAYS = "월화수목금토일"


def _kst_now(now=None):
    now = datetime.now(timezone.utc) if now is None else now
    now = now.astimezone(KST)
    return now.strftime("%m/%d") + "(" + _WEEKDAYS[now.weekday()] + ") " + now.strftime("%H:%M")


def _run_url(env):
    """검증된 값으로만 실행 로그 주소를 만든다. 하나라도 형식이 어긋나면 빈 문자열."""
    server = env.get("GITHUB_SERVER_URL") or "https://github.com"
    repo = env.get("GITHUB_REPOSITORY", "")
    run_id = env.get("GITHUB_RUN_ID", "")
    if not all(isinstance(value, str) for value in (server, repo, run_id)):
        return ""
    if not (re.fullmatch(r"https://[A-Za-z0-9.-]{1,100}", server)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}", repo)
            and re.fullmatch(r"[0-9]{1,30}", run_id)):
        return ""
    return server + "/" + repo + "/actions/runs/" + run_id


def build_message(env, now=None):
    """고정 문구 + 검증된 실행 번호·주소만 담는다(사용자 입력 없음). HTML."""
    lines = ["🔴 <b>[ATR] 워크플로 실패</b>", _kst_now(now), "",
             "GitHub Actions 실행이 실패했습니다."]
    run_id = env.get("GITHUB_RUN_ID", "")
    if isinstance(run_id, str) and re.fullmatch(r"[0-9]{1,30}", run_id):
        lines.append("실행 번호: <code>" + run_id + "</code>")
    url = _run_url(env)
    if url:
        lines += ["", '<a href="' + url + '">실행 로그 보기</a>']
    return "\n".join(lines)


def _plain(message):
    return re.sub(r"<[^>]*>", "", message).replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _post(opener, token, fields):
    payload = urllib.parse.urlencode(fields).encode("utf-8")
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/sendMessage", data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
    )
    return opener.open(request, timeout=REQUEST_TIMEOUT)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        return None


def send_failure_notice(env, *, opener=None):
    """Return only a fixed status; never print exception, response, URL or secret."""
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    chat = env.get("TELEGRAM_CHAT_ID", "")
    if not isinstance(token, str) or not isinstance(chat, str) or not token.strip() or not chat.strip():
        return "not_configured"
    token, chat = token.strip(), chat.strip()
    if (len(token) > 256 or not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token)
            or len(chat) > 128 or any(ord(char) < 32 or ord(char) == 127 for char in chat)):
        return "invalid_configuration"
    message = build_message(env)
    fields = {"chat_id": chat, "text": message, "parse_mode": "HTML",
              "link_preview_options": '{"is_disabled": true}'}
    try:
        if opener is None:
            opener = urllib.request.build_opener(_NoRedirect())
        for attempt in range(2):
            # 400 은 서식 파싱 실패로 확정된 미전송이다 — 평문으로 한 번만 다시 보낸다.
            # 그 밖의 실패(시간 초과 등)는 전송 여부가 모호하므로 다시 보내지 않는다.
            try:
                with _post(opener, token, fields) as response:
                    status = response.getcode()
                    body = response.read(MAX_RESPONSE_BYTES + 1) if status == 200 else b""
            except urllib.error.HTTPError as error:
                status = error.code
                if status != 400:
                    raise
            if status == 400 and attempt == 0:
                fields = {"chat_id": chat, "text": _plain(message),
                          "link_preview_options": '{"is_disabled": true}'}
                continue
            if status != 200:
                return "failed"
            break
        if len(body) > MAX_RESPONSE_BYTES:
            return "failed"
        result = json.loads(body)
        return "sent" if isinstance(result, dict) and result.get("ok") is True else "failed"
    except Exception:
        return "failed"


def main(env=None):
    status = send_failure_notice(os.environ if env is None else env)
    print("workflow_failure_notice=" + status)
    return 0 if status == "sent" else 1


if __name__ == "__main__":
    raise SystemExit(main())
