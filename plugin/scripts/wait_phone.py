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


def parent_pids():
    """由近到遠的祖先程序 (pid, 執行檔名稱)。"""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PE(ctypes.Structure):
            _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                        ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                        ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                        ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                        ("dwFlags", wintypes.DWORD), ("szExeFile", ctypes.c_char * 260)]
        k32 = ctypes.windll.kernel32
        k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
        procs, pe = {}, PE()
        pe.dwSize = ctypes.sizeof(PE)
        ok = k32.Process32First(wintypes.HANDLE(snap), ctypes.byref(pe))
        while ok:
            procs[pe.th32ProcessID] = (pe.th32ParentProcessID, pe.szExeFile.decode("mbcs", "replace"))
            ok = k32.Process32Next(wintypes.HANDLE(snap), ctypes.byref(pe))
        k32.CloseHandle(wintypes.HANDLE(snap))
        lookup = procs.get
    else:
        import subprocess

        def lookup(pid):
            out = subprocess.run(["ps", "-o", "ppid=,comm=", "-p", str(pid)],
                                 capture_output=True, text=True).stdout.split(None, 1)
            return (int(out[0]), out[1].strip()) if len(out) == 2 else None
    chain, pid = [], os.getpid()
    for _ in range(12):
        info = lookup(pid)
        if not info or not info[0] or info[0] == pid:
            break
        pid = info[0]
        parent = lookup(pid)
        chain.append((pid, parent[1] if parent else ""))
    return chain


def find_claude_pid() -> int:
    """Claude Code CLI 的 PID：hook 環境不一定有 CLAUDE_PID，沒有就往上找名稱是 claude 的祖先程序。
    daemon 靠它判斷視窗是否真的關了；找不到回傳 0（daemon 退回用輪詢判斷）。"""
    pid = os.environ.get("CLAUDE_PID") or ""
    if pid.isdigit():
        return int(pid)
    try:
        for p, name in parent_pids():
            if os.path.basename(name).lower().split(".")[0] in ("claude", "node"):
                return p
    except Exception:
        pass
    return 0


def main():
    ev = json.loads(sys.stdin.buffer.read() or b"{}")
    if ev.get("agent_id") or unattended():
        return 0
    claude_pid = find_claude_pid()
    query = urllib.parse.urlencode({"sid": ev.get("session_id", "?"), "cwd": ev.get("cwd", ""),
                                    "wid": secrets.token_hex(6), "pid": claude_pid})
    url = f"http://127.0.0.1:{load_config()['port']}/wait?{query}"
    end = time.time() + 12 * 3600
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
