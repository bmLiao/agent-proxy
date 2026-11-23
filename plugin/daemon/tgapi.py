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
    "port": 47821,
    "edit_interval": 3,       # 卡片 edit 最小間隔秒數
    "notify_on_done": True,   # 完成時刪卡重發（手機會響）；False 只 edit
    "idle_exit": 600,         # 沒有任何 session 後幾秒自動結束
    "away_timeout": 600,      # away 時手機幾秒沒回應就退回電腦確認
}


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
