"""agent-proxy daemon：接收 Claude Code hooks（HTTP），在 Telegram 維持每個 session 一張卡片。

由 scripts/ensure_daemon.py 在 SessionStart 時自動拉起；綁定固定 port 當作單一實例鎖；
所有 session 結束 idle_exit 秒後自行結束。log：<CLAUDE_PLUGIN_DATA>/daemon.log
"""
import json
import logging
import os
import queue
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tgapi import DATA_DIR, MAX_LEN, TG, load_config, load_json, save_json  # noqa: E402

DATA_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(filename=str(DATA_DIR / "daemon.log"), encoding="utf-8",
                    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("daemon")

CFG = load_config()
tg = TG(CFG["bot_token"])
CHAT = (CFG["allowed_chat_ids"] or [None])[0]
CARDS_FILE = DATA_DIR / "cards.json"  # session_id -> message_id（daemon 重啟後繼續 edit 同一張）
SEP = "━━━━━━━━━━━━"
VERSION = str(os.path.getmtime(__file__)) + "|" + os.path.abspath(__file__)  # 程式更新後 ensure_daemon 會換掉舊的


# ---------------------------------------------------------------- 卡片
class Session:
    def __init__(self, sid: str, cwd: str):
        self.sid, self.cwd = sid, cwd
        self.status = "idle"        # idle / busy / done / ended
        self.prompt = ""
        self.started = self.finished = 0.0
        self.tools = 0
        self.todos: dict = {}       # id -> (status, text)，保持插入順序
        self.reply = ""
        self.note = ""
        self.msg_id = load_json(CARDS_FILE, {}).get(sid)
        self.dirty = False
        self.last_edit = 0.0
        self.last_seen = time.time()
        self.truncated = False

    @property
    def project(self) -> str:
        return os.path.basename(self.cwd.rstrip("\\/")) or self.cwd

    def render(self) -> str:
        icon = {"idle": "💤 閒置", "busy": "🔄 執行中", "done": "✅ 完成", "ended": "⚫ 已結束"}
        lines = [f"🟢 {self.project} · {icon[self.status]}"]
        if self.prompt:
            lines += [SEP, f"👤 {clip(self.prompt, 600)}"]
        body = []
        if self.started:
            elapsed = fmt_secs((self.finished or time.time()) - self.started)
            head = "⏱ " if self.status == "done" else "🔄 "
            body.append(f"{head}{elapsed} · 工具 {self.tools} 次")
        marks = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
        body += [f"{marks.get(st, '•')} {clip(text, 120)}" for st, text in self.todos.values()]
        if self.note:
            body.append(self.note)
        if body:
            lines += [SEP, *body]
        text = clip("\n".join(lines), MAX_LEN)
        self.truncated = False
        if self.status == "done" and self.reply:
            room = MAX_LEN - len(text) - 2
            self.truncated = len(self.reply) > room
            text += "\n\n" + (clip(self.reply, room) if self.truncated else self.reply)
        return text

    def markup(self) -> dict:
        """回覆被截斷時附「看全文」按鈕。"""
        if not self.truncated:
            return {"inline_keyboard": []}
        return {"inline_keyboard": [[{"text": "📄 看全文", "callback_data": f"full:{self.sid}"}]]}


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


SESSIONS: dict = {}
lock = threading.RLock()


def save_cards():
    save_json(CARDS_FILE, {sid: s.msg_id for sid, s in SESSIONS.items() if s.msg_id})


def send_card(s: Session, notify: bool):
    """發新卡；若有舊卡先刪除（刪除失敗不影響，例如已超過 48 小時）。"""
    if s.msg_id:
        tg.call("deleteMessage", chat_id=CHAT, message_id=s.msg_id)
    res = tg.call("sendMessage", chat_id=CHAT, text=s.render(), disable_notification=not notify,
                  reply_markup=s.markup(), link_preview_options={"is_disabled": True})
    s.msg_id = (res.get("result") or {}).get("message_id")
    s.dirty, s.last_edit = False, time.time()
    save_cards()


def edit_card(s: Session):
    if not s.msg_id:
        return send_card(s, notify=False)
    res = tg.call("editMessageText", chat_id=CHAT, message_id=s.msg_id, text=s.render(),
                  reply_markup=s.markup(), link_preview_options={"is_disabled": True})
    s.dirty, s.last_edit = False, time.time()
    desc = str(res.get("description", ""))
    if not res.get("ok") and "not modified" not in desc:  # 卡片被刪掉等 → 換一張新的
        s.msg_id = None
        send_card(s, notify=False)


def refresh(s: Session, now: bool = False, notify: bool = False):
    if CHAT is None:
        return
    if notify:
        send_card(s, notify=True)
    elif now:
        edit_card(s)
    else:
        s.dirty = True  # 交給 ticker 依 edit_interval 節流


def ticker():
    while True:
        time.sleep(1)
        with lock:
            for s in list(SESSIONS.values()):
                if s.dirty and time.time() - s.last_edit >= CFG["edit_interval"]:
                    safe(edit_card, s)


# ---------------------------------------------------------------- hooks
def task_update(s: Session, tool: str, inp: dict, resp):
    if tool == "TodoWrite":
        s.todos = {str(i): (t.get("status"), t.get("content", ""))
                   for i, t in enumerate(inp.get("todos") or [])}
    elif tool == "TaskCreate":
        r = resp if isinstance(resp, dict) else {}
        tid = str((r.get("task") or {}).get("id") or r.get("id") or r.get("taskId") or len(s.todos) + 1)
        s.todos[tid] = ("pending", inp.get("subject") or inp.get("description") or "")
    elif tool == "TaskUpdate":
        tid = str(inp.get("taskId") or inp.get("id") or "")
        if tid in s.todos:
            st, text = s.todos[tid]
            if inp.get("status") == "deleted":
                s.todos.pop(tid)
            else:
                s.todos[tid] = (inp.get("status") or st, inp.get("subject") or text)


def on_hook(ev: dict):
    name, sid = ev.get("hook_event_name", ""), ev.get("session_id") or "?"
    log.info("hook %s %s %s", sid[:8], name, ev.get("tool_name") or "")
    if ev.get("agent_id"):  # 子代理內部事件不轉播
        return
    with lock:
        s = SESSIONS.get(sid)
        if not s:
            s = SESSIONS[sid] = Session(sid, ev.get("cwd") or "")
        s.cwd, s.last_seen = ev.get("cwd") or s.cwd, time.time()
        if name == "UserPromptSubmit":
            prompt = (ev.get("prompt") or "").strip()
            if not prompt or prompt.startswith("<"):  # 系統自動訊息不算輸入
                return
            s.status, s.prompt, s.reply, s.note = "busy", prompt, "", ""
            s.started, s.finished, s.tools, s.todos = time.time(), 0.0, 0, {}
            refresh(s, now=True)
        elif name == "PostToolUse" and s.status == "busy":
            s.tools += 1
            task_update(s, ev.get("tool_name") or "", ev.get("tool_input") or {},
                        ev.get("tool_response"))
            refresh(s)
        elif name == "Notification":
            if ev.get("notification_type") not in ("idle_prompt", "permission_prompt") and s.prompt:
                s.note = f"🔔 {clip(ev.get('message', ''), 200)}"
                refresh(s)
        elif name == "Stop" and s.prompt:
            s.status, s.finished = "done", time.time()
            s.reply, s.note = (ev.get("last_assistant_message") or "").strip(), ""
            refresh(s, notify=CFG["notify_on_done"], now=True)
            log.info("card %s msg=%s\n%s", sid[:8], s.msg_id, s.render()[:300])
        elif name == "SessionEnd":
            if s.msg_id:
                s.status = "ended"
                refresh(s, now=True)
            SESSIONS.pop(sid, None)
            save_cards()


HOOK_QUEUE: "queue.Queue[dict]" = queue.Queue()


def hook_worker():
    while True:  # 依序處理，卡片內容與 CLI 一致
        safe(on_hook, HOOK_QUEUE.get())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/quit":  # ensure_daemon 發現版本不符時要求退出
            self.reply(200, {"ok": True})
            log.info("收到 /quit，daemon 結束（版本更新）")
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
            return
        self.reply(200, {"ok": True, "pid": os.getpid(), "version": VERSION})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.reply(200, {})  # 不做決定、立即放行；處理在背景佇列
        try:
            HOOK_QUEUE.put(json.loads(body or b"{}"))
        except Exception:
            log.warning("無法解析 hook 內容")

    def reply(self, code: int, obj: dict):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- Telegram 輸入（M1：只有 /status、/id）
def authorized(chat_id, user_id) -> bool:
    return chat_id in CFG["allowed_chat_ids"] and (
        not CFG["allowed_user_ids"] or user_id in CFG["allowed_user_ids"])


def send_long(chat_id, text: str):
    """完整內容依 Telegram 上限切段送出。"""
    while text:
        cut = len(text)
        if cut > MAX_LEN:
            nl = text.rfind("\n", 0, MAX_LEN)
            cut = nl if nl > MAX_LEN // 2 else MAX_LEN
        tg.call("sendMessage", chat_id=chat_id, text=text[:cut], link_preview_options={"is_disabled": True})
        text = text[cut:].lstrip("\n")


def on_callback(cq: dict):
    chat_id = cq["message"]["chat"]["id"]
    if not authorized(chat_id, cq["from"]["id"]):
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="你沒有操作權限")
        return
    kind, _, val = cq.get("data", "").partition(":")
    s = SESSIONS.get(val)
    if kind == "full" and s and s.reply:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        send_long(chat_id, f"📄 {s.project} 完整回覆\n\n{s.reply}")
    else:
        tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="內容已不存在")


def on_update(u: dict):
    if "callback_query" in u:
        return on_callback(u["callback_query"])
    msg = u.get("message")
    if not msg or "text" not in msg:
        return
    chat_id, user_id, text = msg["chat"]["id"], (msg.get("from") or {}).get("id"), msg["text"].strip()
    if not authorized(chat_id, user_id):
        if text.startswith(("/start", "/id")):
            tg.call("sendMessage", chat_id=chat_id,
                    text=f"chat id: {chat_id}\nuser id: {user_id}\n請加入設定檔的 allowed_chat_ids / allowed_user_ids")
        return
    if text.startswith(("/status", "/id", "/start")):
        with lock:
            rows = [f"• {s.project} · {s.status}" for s in SESSIONS.values()] or ["（沒有 session）"]
        tg.call("sendMessage", chat_id=chat_id, disable_notification=True,
                text="🖥️ Session：\n" + "\n".join(rows))


def poll_loop():
    res = tg.call("getUpdates", offset=-1, timeout=0)  # 略過啟動前累積的舊訊息
    offset = ((res.get("result") or [{}])[-1].get("update_id", -1) + 1) if res.get("ok") else None
    while True:
        res = tg.call("getUpdates", _timeout=60, _retry=0, offset=offset, timeout=50,
                      allowed_updates=["message", "callback_query"])
        if not res.get("ok"):
            if "Conflict" in str(res.get("description")):
                log.warning("另一個程式正在使用這個 bot（舊的 relay.py？）")
            time.sleep(3)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            safe(on_update, u)


# ---------------------------------------------------------------- 生命週期
def safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        log.exception("%s 失敗", getattr(fn, "__name__", fn))


def reaper():
    """清掉 6 小時沒動靜的 session；沒有 session 超過 idle_exit 秒就結束 daemon。"""
    empty_since = time.time()
    while True:
        time.sleep(30)
        with lock:
            for sid, s in list(SESSIONS.items()):
                if time.time() - s.last_seen > 6 * 3600:
                    SESSIONS.pop(sid)
            if SESSIONS:
                empty_since = time.time()
        if time.time() - empty_since > CFG["idle_exit"]:
            log.info("沒有 session，daemon 結束")
            os._exit(0)


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), Handler)
    except OSError:
        log.info("port %s 已被佔用，daemon 已在執行", CFG["port"])
        return
    if not CFG["bot_token"] or CHAT is None:
        log.error("設定檔缺少 bot_token 或 allowed_chat_ids：%s", DATA_DIR / "config.json")
    else:
        threading.Thread(target=poll_loop, daemon=True).start()
    for fn in (ticker, hook_worker, reaper):
        threading.Thread(target=fn, daemon=True).start()
    log.info("daemon 啟動 pid=%s port=%s", os.getpid(), CFG["port"])
    server.serve_forever()


if __name__ == "__main__":
    main()
