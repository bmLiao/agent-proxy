"""極簡 Telegram Bot API 客戶端（只用標準庫）與設定讀取。"""
import json
import logging
import os
import pathlib
import time
import urllib.error
import urllib.request

PLUGIN_ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = pathlib.Path(os.environ.get("CLAUDE_PLUGIN_DATA") or PLUGIN_ROOT / ".data")

log = logging.getLogger("tg")
MAX_LEN = 4000  # Telegram 上限 4096（以 UTF-16 計），留餘量給 emoji

DEFAULTS = {
    "bot_token": "",
    "allowed_chat_ids": [],
    "allowed_user_ids": [],   # 非空時只有這些使用者能操作
    "port": 47821,            # 固定：hooks.json 的 URL 也寫死這個 port
    "edit_interval": 3,       # 卡片 edit 最小間隔秒數
    "notify_on_done": True,   # 完成時刪卡重發（手機會響）；False 只 edit
    "idle_exit": 1800,        # 沒有 session 也沒有任何 hook 事件幾秒後自動結束
    "away_timeout": 600,      # away 時手機幾秒沒回應就退回電腦確認
}


def code_version() -> str:
    """daemon 程式內容的雜湊：程式更新才換 daemon；不同設定目錄的同版安裝視為同一版。"""
    import hashlib
    h = hashlib.sha1()
    for name in ("daemon.py", "tgapi.py"):
        h.update((PLUGIN_ROOT / "daemon" / name).read_bytes())
    return h.hexdigest()[:12]


def pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
        if not h:
            return False
        alive = k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT = 還在跑
        k32.CloseHandle(h)
        return alive
    try:
        os.kill(pid, 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False


def unattended() -> bool:
    """claude -p 等非互動 session：不連手機（asyncRewake 會讓 -p 卡住，也不該產生卡片）。"""
    return os.environ.get("CLAUDE_CODE_SESSION_ATTENDED") == "0"


def config_paths():
    """依序讀取，後者覆蓋前者：插件資料夾 → repo 的 local/config.json（開發用）。"""
    return [DATA_DIR / "config.json", PLUGIN_ROOT.parent / "local" / "config.json"]


def ids(value) -> list:
    if isinstance(value, list):
        return [int(v) for v in value]
    return [int(v) for v in str(value).replace(" ", "").split(",") if v]


def load_config() -> dict:
    """預設值 → 設定檔 → 插件設定（/plugin 介面填的 userConfig，以環境變數傳入）。"""
    cfg = dict(DEFAULTS)
    for p in config_paths():
        if p.exists():
            cfg.update(json.loads(p.read_text(encoding="utf-8")))
    for key in ("bot_token", "allowed_chat_ids", "allowed_user_ids"):
        val = (os.environ.get("CLAUDE_PLUGIN_OPTION_" + key.upper())
               or os.environ.get("CLAUDE_PLUGIN_OPTION_" + key))
        if val:
            cfg[key] = val
    cfg["allowed_chat_ids"] = ids(cfg["allowed_chat_ids"])
    cfg["allowed_user_ids"] = ids(cfg["allowed_user_ids"])
    return cfg


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: pathlib.Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class TG:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, _timeout: float = 30, _retry: int = 3, **params) -> dict:
        req = urllib.request.Request(self.base + method, data=json.dumps(params).encode("utf-8"),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=_timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            try:
                j = json.loads(body)
            except Exception:
                j = {"ok": False, "description": body}
            retry_after = (j.get("parameters") or {}).get("retry_after")
            if retry_after and _retry > 0:
                time.sleep(retry_after + 0.5)
                return self.call(method, _timeout, _retry - 1, **params)
            if "not modified" not in str(j.get("description")):
                log.warning("TG %s 失敗: %s", method, j.get("description"))
            return j
        except Exception as e:  # 網路錯誤
            if _retry > 0 and method != "getUpdates":
                time.sleep(2)
                return self.call(method, _timeout, _retry - 1, **params)
            log.warning("TG %s 例外: %s", method, e)
            return {"ok": False, "description": str(e)}
