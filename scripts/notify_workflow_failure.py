"""Dependency-independent fallback notification for an ordinary workflow failure.

This can duplicate an application failure message. It does not monitor absent
workflow runs or guarantee delivery after job cancellation/timeouts. A network
timeout is ambiguous, so this helper does not retry or claim exactly-once send.
"""
import json
import os
import re
import urllib.parse
import urllib.request


MAX_RESPONSE_BYTES = 64 * 1024
REQUEST_TIMEOUT = 10


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
    message = "ATR Monitor 워크플로가 실패했습니다. GitHub Actions에서 실행 결과를 확인해 주세요."
    run_id = env.get("GITHUB_RUN_ID", "")
    if isinstance(run_id, str) and re.fullmatch(r"[0-9]{1,30}", run_id):
        message += "\n실행 번호: " + run_id
    try:
        payload = urllib.parse.urlencode({"chat_id": chat, "text": message}).encode("utf-8")
        request = urllib.request.Request(
            "https://api.telegram.org/bot" + token + "/sendMessage", data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        if opener is None:
            opener = urllib.request.build_opener(_NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if response.getcode() != 200:
                return "failed"
            body = response.read(MAX_RESPONSE_BYTES + 1)
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
