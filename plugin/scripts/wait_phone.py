"""asyncRewake hook（SessionStart、Stop）：在背景向 daemon 等手機訊息。

收到 → 印到 stderr、exit 2 → Claude Code 叫醒 Claude 處理。
被新的 waiter 取代、session 結束、或超過 12 小時 → exit 0。daemon 暫時連不上就重試。
"""
import json
import pathlib
import secrets
import sys
import time
import urllib.parse
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
from tgapi import load_config  # noqa: E402


def main():
    ev = json.loads(sys.stdin.buffer.read() or b"{}")
    if ev.get("agent_id"):
        return 0
    query = urllib.parse.urlencode({"sid": ev.get("session_id", "?"), "cwd": ev.get("cwd", ""),
                                    "wid": secrets.token_hex(6)})
    url = f"http://127.0.0.1:{load_config()['port']}/wait?{query}"
    end = time.time() + 12 * 3600
    while time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=70) as r:
                res = json.loads(r.read())
        except Exception:
            time.sleep(2)
            continue
        if res.get("text"):
            sys.stderr.write(res["text"])
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
