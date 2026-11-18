"""把 cli_hook.py 加入 Claude Code 的 settings.json。

用法：
  python install_hook.py               # 全域：$CLAUDE_CONFIG_DIR 或 ~/.claude 的 settings.json
  python install_hook.py <settings路徑>  # 指定檔案，例如某專案的 .claude/settings.local.json
  python install_hook.py --print       # 只輸出 hooks JSON（可給 claude --settings 使用）
  python install_hook.py --uninstall [settings路徑]
會先備份成 settings.json.bak。安裝後，已經開著的 CLI 要重開才會載入。
"""
import json
import os
import pathlib
import shutil
import sys

HOOK = pathlib.Path(__file__).resolve().parent / "cli_hook.py"
COMMAND = f'"{sys.executable}" "{HOOK}"'.replace("\\", "/")
EVENTS = {  # 事件 -> hook 逾時秒數（PermissionRequest 要等手機）
    "SessionStart": 10, "SessionEnd": 10, "UserPromptSubmit": 10, "PostToolUse": 10,
    "Notification": 10, "Stop": 10, "PermissionRequest": 3600,
}
OURS = ("cli_hook.py", "notify_hook.py")


def hooks_block():
    return {ev: [{"hooks": [{"type": "command", "command": COMMAND, "timeout": t}]}]
            for ev, t in EVENTS.items()}


def main():
    args = sys.argv[1:]
    if "--print" in args:
        print(json.dumps({"hooks": hooks_block()}, indent=2))
        return
    uninstall = "--uninstall" in args
    args = [a for a in args if a != "--uninstall"]
    if args:
        path = pathlib.Path(args[0])
    else:
        base = os.environ.get("CLAUDE_CONFIG_DIR") or pathlib.Path.home() / ".claude"
        path = pathlib.Path(base) / "settings.json"

    settings = {}
    if path.exists():
        shutil.copy2(path, path.with_name(path.name + ".bak"))
        settings = json.loads(path.read_text(encoding="utf-8") or "{}")
    hooks = settings.setdefault("hooks", {})

    for ev in list(hooks):  # 先移除舊的本專案 hook
        groups = hooks[ev]
        for g in groups:
            g["hooks"] = [h for h in g.get("hooks", [])
                          if not any(o in h.get("command", "") for o in OURS)]
        hooks[ev] = [g for g in groups if g.get("hooks")]
        if not hooks[ev]:
            hooks.pop(ev)
    if not uninstall:
        for ev, groups in hooks_block().items():
            hooks.setdefault(ev, []).extend(groups)
    if not hooks:
        settings.pop("hooks", None)

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    print(("已移除" if uninstall else "已安裝") + f" hook -> {path}")


if __name__ == "__main__":
    main()
