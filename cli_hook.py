"""Claude Code hook：讓每個互動式 CLI 向 relay 登記並回報事件。

掛在 SessionStart / SessionEnd / UserPromptSubmit / PostToolUse / PermissionRequest /
Notification / Stop（install_hook.py 會幫你加）。
- 找出所屬 CLI 的行程 pid（往上找 claude.exe / node.exe），relay 用它辨識 CLI 並打字進去
- 把 hook 輸入轉給 relay；PermissionRequest 會等手機決定，再把結果印到 stdout 給 Claude
relay 沒開、出錯、逾時都安靜地 exit 0，不影響 CLI 本身。
"""
import ctypes
import json
import os
import pathlib
import sys
import urllib.request
from ctypes import wintypes

RELAY_INFO = pathlib.Path(__file__).resolve().parent / ".runtime" / "relay.json"
CLI_EXES = {"claude.exe", "node.exe", "claude"}


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_void_p),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260)]


def find_cli_pid():
    if os.name != "nt":
        return os.getppid()
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    procs = {}
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(e)
    ok = k32.Process32FirstW(snap, ctypes.byref(e))
    while ok:
        procs[e.th32ProcessID] = (e.th32ParentProcessID, e.szExeFile.lower())
        ok = k32.Process32NextW(snap, ctypes.byref(e))
    k32.CloseHandle(snap)
    pid = os.getpid()
    for _ in range(12):
        ppid = procs.get(pid, (0, ""))[0]
        if not ppid or ppid not in procs:
            return None
        if procs[ppid][1] in CLI_EXES:
            return ppid
        pid = ppid
    return None


def main():
    data = json.loads(sys.stdin.buffer.read().decode("utf-8") or "{}")
    try:
        info = json.loads(RELAY_INFO.read_text(encoding="utf-8"))
    except Exception:
        return  # relay 沒在跑
    data["cli_pid"] = find_cli_pid()
    waiting = data.get("hook_event_name") == "PermissionRequest"
    req = urllib.request.Request(
        info["url"], data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Token": info["token"]})
    with urllib.request.urlopen(req, timeout=3600 if waiting else 5) as r:
        out = json.loads(r.read() or b"{}")
    if out:
        sys.stdout.write(json.dumps(out))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
