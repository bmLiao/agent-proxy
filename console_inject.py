"""把文字當作鍵盤輸入，打進另一個行程的 Windows 主控台（Claude CLI 的終端機）。

用法：python console_inject.py <pid> <文字>      # 文字後自動按 Enter
      python console_inject.py <pid> --keys esc   # 送特殊鍵（esc / enter）
必須以「無主控台」方式啟動（relay 用 DETACHED_PROCESS），才能 AttachConsole 到目標。
成功 exit 0，失敗 exit 1 並在 stderr 印出原因。
"""
import ctypes
import sys
import time
from ctypes import wintypes

k32 = ctypes.WinDLL("kernel32", use_last_error=True)

KEY_EVENT = 0x0001
VK_RETURN, VK_ESCAPE = 0x0D, 0x1B
GENERIC_RW = 0x80000000 | 0x40000000
FILE_SHARE_RW = 0x1 | 0x2
OPEN_EXISTING = 3


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [("bKeyDown", wintypes.BOOL), ("wRepeatCount", wintypes.WORD),
                ("wVirtualKeyCode", wintypes.WORD), ("wVirtualScanCode", wintypes.WORD),
                ("uChar", wintypes.WCHAR), ("dwControlKeyState", wintypes.DWORD)]


class _EVENT(ctypes.Union):
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_pad", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EVENT)]


def records_for(chars):
    recs = []
    for ch, vk in chars:
        for down in (True, False):
            r = INPUT_RECORD()
            r.EventType = KEY_EVENT
            r.Event.KeyEvent.bKeyDown = down
            r.Event.KeyEvent.wRepeatCount = 1
            r.Event.KeyEvent.wVirtualKeyCode = vk
            r.Event.KeyEvent.uChar = ch
            recs.append(r)
    return recs


def write(handle, chars):
    recs = records_for(chars)
    arr = (INPUT_RECORD * len(recs))(*recs)
    n = wintypes.DWORD()
    if not k32.WriteConsoleInputW(handle, arr, len(recs), ctypes.byref(n)):
        raise OSError(f"WriteConsoleInputW 失敗 ({ctypes.get_last_error()})")


def main():
    pid, payload = int(sys.argv[1]), sys.argv[2]
    k32.FreeConsole()
    if not k32.AttachConsole(pid):
        sys.exit(f"AttachConsole({pid}) 失敗 ({ctypes.get_last_error()})")
    k32.CreateFileW.restype = wintypes.HANDLE
    h = k32.CreateFileW("CONIN$", GENERIC_RW, FILE_SHARE_RW, None, OPEN_EXISTING, 0, None)
    if not h or h == wintypes.HANDLE(-1).value:
        sys.exit(f"開啟 CONIN$ 失敗 ({ctypes.get_last_error()})")

    if payload == "--keys":
        key = sys.argv[3].lower()
        vk, ch = {"esc": (VK_ESCAPE, "\x1b"), "enter": (VK_RETURN, "\r")}[key]
        write(h, [(ch, vk)])
        return
    text = " ".join(payload.splitlines())  # 換行會被當成送出，改成空白
    write(h, [(c, 0) for c in text])
    time.sleep(0.4)  # 讓 TUI 把文字當成輸入而不是貼上的一部分，再按 Enter
    write(h, [("\r", VK_RETURN)])


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        sys.exit(str(e))
