"""用 Telegram 同步的方式啟動 Claude Code：python cctg.py [claude 的其他參數...]

只影響這次啟動的 CLI，不改任何全域設定：
- --mcp-config：掛上 channel.py（Telegram ⇄ 這個 session）
- --settings：HTTP hooks 打到 channel.py（電腦端輸入、工具呼叫、回覆推到手機）
- --dangerously-load-development-channels：Channels 研究預覽期間自訂 channel 必須的旗標，
  每次啟動 Claude Code 會請你確認一次
"""
import json
import pathlib
import secrets
import shutil
import signal
import socket
import subprocess
import sys

BASE = pathlib.Path(__file__).resolve().parent
RUNTIME = BASE / ".runtime"
HOOK_EVENTS = ("UserPromptSubmit", "PostToolUse", "Notification", "Stop")


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    claude = shutil.which("claude")
    if not claude:
        sys.exit("找不到 claude 指令")
    port, token = free_port(), secrets.token_hex(16)
    RUNTIME.mkdir(exist_ok=True)
    mcp_file = RUNTIME / f"mcp-{port}.json"
    settings_file = RUNTIME / f"settings-{port}.json"

    mcp_file.write_text(json.dumps({"mcpServers": {"agent-proxy": {
        "command": sys.executable,
        "args": [str(BASE / "channel.py")],
        "env": {"AP_PORT": str(port), "AP_TOKEN": token, "PYTHONUTF8": "1"},
    }}}, indent=2), encoding="utf-8")
    hook = {"type": "http", "url": f"http://127.0.0.1:{port}/hook",
            "headers": {"X-Token": token}, "timeout": 5}
    settings_file.write_text(json.dumps(
        {"hooks": {ev: [{"hooks": [hook]}] for ev in HOOK_EVENTS}}, indent=2), encoding="utf-8")

    signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl+C 交給 claude 處理，不要殺掉啟動器
    try:
        code = subprocess.call([claude, "--mcp-config", str(mcp_file),
                                "--settings", str(settings_file),
                                "--dangerously-load-development-channels", "server:agent-proxy",
                                *sys.argv[1:]])
    finally:
        mcp_file.unlink(missing_ok=True)
        settings_file.unlink(missing_ok=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
