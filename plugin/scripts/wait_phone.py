"""asyncRewake hook（SessionStart、Stop）：在背景向 daemon 等手機訊息。

收到 → 印到 stderr、exit 2 → Claude Code 叫醒 Claude 處理。
被新的 waiter 取代、session 結束、或超過 12 小時 → exit 0。daemon 暫時連不上就重試。
"""
import json
import os
import pathlib
import secrets
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
from tgapi import load_config, pid_alive, unattended  # noqa: E402


def main():
    ev = json.loads(sys.stdin.buffer.read() or b"{}")
    if ev.get("agent_id") or unattended():
        return 0
    query = urllib.parse.urlencode({"sid": ev.get("session_id", "?"), "cwd": ev.get("cwd", ""),
                                    "wid": secrets.token_hex(6)})
    url = f"http://127.0.0.1:{load_config()['port']}/wait?{query}"
    end = time.time() + 12 * 3600
    claude_pid = int(os.environ.get("CLAUDE_PID") or 0)
    while time.time() < end:
        if claude_pid and not pid_alive(claude_pid):  # CLI 已關閉
            return 0
        try:
            with urllib.request.urlopen(url, timeout=70) as r:
                res = json.loads(r.read())
        except Exception:
            time.sleep(2)
            continue
        if res.get("text"):
            # 以 UTF-8 位元組輸出：Windows 上 sys.stderr 預設是系統碼頁（cp950），中文會變亂碼
            sys.stderr.buffer.write(res["text"].encode("utf-8"))
            sys.stderr.buffer.flush()
            return 2
        if res.get("exit"):
            return 0
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except Exception:
        code = 0
    sys.exit(code)
