"""極簡 Telegram Bot API 客戶端（只用標準庫）與共用設定讀取。"""
import json
import logging
import pathlib
import time
import urllib.error
import urllib.request

BASE = pathlib.Path(__file__).resolve().parent
CONFIG_FILE = BASE / "config.json"         # 一般設定（可進版控）
LOCAL_DIR = BASE / "local"                 # 私密/本機資料夾（不進版控）
LOCAL_FILE = LOCAL_DIR / "config.json"     # 機器人私密資訊：bot_token、白名單 id
RUNTIME = BASE / ".runtime"
RELAY_INFO = RUNTIME / "relay.json"  # relay 啟動時寫入 {url, token}，給 cli_hook.py 找到 relay

log = logging.getLogger("tg")
MAX_LEN = 4000  # Telegram 單則上限 4096


def load_config() -> dict:
    """讀 config.json，再用 config.local.json 覆蓋（私密資訊只放 local）。"""
    cfg = json.loads(CONFIG_FILE.read_text(encoding="utf-8")) if CONFIG_FILE.exists() else {}
    if LOCAL_FILE.exists():
        cfg.update(json.loads(LOCAL_FILE.read_text(encoding="utf-8")))
    cfg.setdefault("bot_token", "")
    cfg.setdefault("allowed_chat_ids", [])
    cfg.setdefault("allowed_user_ids", [])  # 非空時，只有這些使用者能操作（群組內其他人只能看）
    cfg.setdefault("flush_interval", 3)
    cfg.setdefault("show_tool_calls", True)
    cfg.setdefault("cli_approval_timeout", 600)  # 接管 CLI 時手機授權的等待秒數；逾時改回電腦端確認
    return cfg


def last_assistant_text(transcript_path: str, limit: int = 3000) -> str:
    """從 Claude 的 transcript JSONL 取出最後一段助理文字。"""
    try:
        p = pathlib.Path(transcript_path)
        with p.open("rb") as f:
            f.seek(max(0, p.stat().st_size - 400_000))
            lines = f.read().decode("utf-8", "replace").splitlines()
        for line in reversed(lines):
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if ev.get("type") != "assistant":
                continue
            texts = [b.get("text", "") for b in ev.get("message", {}).get("content", [])
                     if isinstance(b, dict) and b.get("type") == "text"]
            text = "\n".join(t for t in texts if t.strip()).strip()
            if text:
                return text if len(text) <= limit else "…" + text[-limit:]
    except Exception:
        pass
    return ""


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: pathlib.Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def split_text(text: str, size: int = MAX_LEN):
    text = text or "(空)"
    while text:
        if len(text) <= size:
            yield text
            return
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        yield text[:cut]
        text = text[cut:].lstrip("\n")


class TG:
    def __init__(self, token: str):
        self.base = f"https://api.telegram.org/bot{token}/"

    def call(self, method: str, _timeout: float = 30, _retry: int = 3, **params):
        data = json.dumps(params).encode("utf-8")
        req = urllib.request.Request(
            self.base + method, data=data, headers={"Content-Type": "application/json"}
        )
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
            log.warning("TG %s 失敗: %s", method, j.get("description"))
            return j
        except Exception as e:  # 網路錯誤
            if _retry > 0 and method != "getUpdates":
                time.sleep(2)
                return self.call(method, _timeout, _retry - 1, **params)
            log.warning("TG %s 例外: %s", method, e)
            return {"ok": False, "description": str(e)}

    def send(self, chat_id, text: str, silent: bool = False, buttons=None):
        """傳送文字（自動切段）。buttons: [[(文字, callback_data), ...], ...] 掛在最後一段。"""
        chunks = list(split_text(text))
        res = None
        for i, chunk in enumerate(chunks):
            params = {"chat_id": chat_id, "text": chunk, "disable_notification": silent,
                      "link_preview_options": {"is_disabled": True}}
            if buttons and i == len(chunks) - 1:
                params["reply_markup"] = {"inline_keyboard": [
                    [{"text": t, "callback_data": d} for t, d in row] for row in buttons
                ]}
            res = self.call("sendMessage", **params)
        return res
