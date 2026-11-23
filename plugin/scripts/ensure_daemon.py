"""SessionStart hook：確保 daemon 在跑（HTTP hooks 才不會連線失敗），互動 session 再向它註冊。
任何錯誤都安靜 exit 0。"""
import json
import os
import pathlib
import subprocess
import sys
import time
import urllib.request

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "daemon"))
from tgapi import DATA_DIR, code_version, load_config, unattended  # noqa: E402

BASE = f"http://127.0.0.1:{load_config()['port']}"
URL = BASE + "/hook"
VERSION = code_version()


def get(path: str):
    try:
        with urllib.request.urlopen(BASE + path, timeout=2) as r:
            return json.loads(r.read())
    except Exception:
        return None


def post(body: bytes, timeout: float = 2) -> bool:
    try:
        req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def spawn():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": open(DATA_DIR / "daemon.err.log", "ab"), "cwd": str(ROOT / "daemon")}
    if os.name == "nt":  # 脫離 CLI 的主控台與 job，CLI 關掉 daemon 也不會被一起砍掉
        flags = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED | NEW_PROCESS_GROUP | NO_WINDOW
        try:
            subprocess.Popen([sys.executable, "daemon.py"], creationflags=flags | 0x01000000, **kw)
            return  # 0x01000000 = BREAKAWAY_FROM_JOB（job 不允許時改用下面）
        except OSError:
            pass
        subprocess.Popen([sys.executable, "daemon.py"], creationflags=flags, **kw)
    else:
        subprocess.Popen([sys.executable, "daemon.py"], start_new_session=True, **kw)


def main():
    body = sys.stdin.buffer.read() or b"{}"
    json.loads(body)
    if unattended():  # claude -p 等：只確保 daemon 在跑，不註冊（不產生卡片、不等手機訊息）
        body = b'{"hook_event_name": "Ping"}'
    info = get("/")
    if info and info.get("version") == VERSION and post(body):
        return
    if info:  # 舊版 daemon 還佔著 port → 請它退出
        get("/quit")
        for _ in range(20):
            time.sleep(0.1)
            if get("/") is None:
                break
    spawn()
    for _ in range(30):  # 最多等 3 秒 daemon 起來
        time.sleep(0.1)
        if post(body):
            return


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
