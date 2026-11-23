"""Claude Code Channel MCP 伺服器：手機 Telegram ⇄ 這一個 CLI session。

由 cctg.py 啟動 claude 時一起掛上（--mcp-config + --dangerously-load-development-channels），
不需要接管、不打字進終端機：
- 手機文字 → notifications/claude/channel → 成為 Claude 的輸入
- 權限確認 → claude/channel/permission：電腦視窗與手機按鈕同時有效，先按先贏
- hooks（HTTP，打到本程式）→ 電腦端輸入、工具呼叫、回合完成的回覆推到手機
stdout 是 MCP 協定專用，log 一律寫檔。CLI 結束（stdin 關閉）時本程式跟著結束。
"""
import json
import logging
import os
import queue
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tgapi import TG, LOCAL_DIR, last_assistant_text, load_config

logging.basicConfig(filename=str(LOCAL_DIR / "channel.log"), encoding="utf-8",
                    level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("channel")

CFG = load_config()
PORT = int(os.environ["AP_PORT"])
TOKEN = os.environ["AP_TOKEN"]
tg = TG(CFG["bot_token"])

INSTRUCTIONS = (
    "The user is also following this session from Telegram on their phone. "
    'Messages they type there arrive as <channel source="agent-proxy" ...>text</channel>; '
    "treat them exactly like a prompt the user typed in the terminal. "
    "Your normal replies are forwarded to the phone automatically, so just answer as usual "
    "(there is no reply tool and you never need one)."
)
PERM_REPLY_RE = re.compile(r"^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$", re.I)

state = {"busy": False, "cwd": os.getcwd(), "sent": set()}  # sent：手機送出的文字，用來過濾回音
PENDING: dict = {}  # request_id -> {chat, msg_id, text, tool}
lock = threading.Lock()


def chats():
    return CFG["allowed_chat_ids"]


def project() -> str:
    return os.path.basename(state["cwd"].rstrip("\\/")) or state["cwd"]


# ---------------------------------------------------------------- MCP（JSON-RPC over stdio）
out_lock = threading.Lock()


def mcp_write(msg: dict):
    data = (json.dumps(msg) + "\n").encode("utf-8")
    with out_lock:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()


def mcp_notify(method: str, params: dict):
    mcp_write({"jsonrpc": "2.0", "method": method, "params": params})


def handle_mcp(msg: dict):
    method, mid = msg.get("method"), msg.get("id")
    if method == "initialize":
        mcp_write({"jsonrpc": "2.0", "id": mid, "result": {
            "protocolVersion": (msg.get("params") or {}).get("protocolVersion", "2025-06-18"),
            "capabilities": {"experimental": {"claude/channel": {},
                                              "claude/channel/permission": {}}},
            "serverInfo": {"name": "agent-proxy", "version": "0.2.0"},
            "instructions": INSTRUCTIONS,
        }})
    elif method == "notifications/claude/channel/permission_request":
        on_permission_request(msg.get("params") or {})
    elif method == "ping":
        mcp_write({"jsonrpc": "2.0", "id": mid, "result": {}})
    elif mid is not None and method:  # 其他請求一律回「不支援」
        mcp_write({"jsonrpc": "2.0", "id": mid,
                   "error": {"code": -32601, "message": f"Method not found: {method}"}})


def mcp_loop():
    for line in sys.stdin.buffer:
        line = line.strip()
        if not line:
            continue
        try:
            handle_mcp(json.loads(line))
        except Exception:
            log.exception("MCP 訊息處理失敗: %r", line[:200])


# ---------------------------------------------------------------- 推送到手機
def describe_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name in ("Bash", "PowerShell"):
        return f"💻 $ {inp.get('command', '')[:400]}"
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        return f"✏️ {name}: {inp.get('file_path') or inp.get('notebook_path', '')}"
    if name == "Read":
        return f"📖 Read: {inp.get('file_path', '')}"
    if name in ("Grep", "Glob"):
        return f"🔎 {name}: {inp.get('pattern', '')}"
    if name == "WebFetch":
        return f"🌐 Fetch: {inp.get('url', '')}"
    if name == "WebSearch":
        return f"🌐 Search: {inp.get('query', '')}"
    if name in ("Task", "Agent"):
        return f"🤖 子代理: {inp.get('description', '')}"
    if name == "TodoWrite":
        marks = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
        return "📋 進度\n" + "\n".join(f"{marks.get(t.get('status'), '•')} {t.get('content', '')}"
                                     for t in inp.get("todos", []))
    return f"🔧 {name}: {json.dumps(inp, ensure_ascii=False)[:300]}"


class Batch:
    """工具呼叫等零碎訊息合併後再送，避免洗版與限流。"""

    def __init__(self):
        self.buf, self.last, self.lock = [], 0.0, threading.Lock()

    def add(self, text: str):
        with self.lock:
            self.buf.append(text)
            big = sum(map(len, self.buf)) > 3000
        if big:
            self.flush()

    def flush(self):
        with self.lock:
            buf, self.buf, self.last = self.buf, [], time.time()
        if buf:
            broadcast("\n\n".join(buf), silent=True)

    def loop(self):
        while True:
            time.sleep(1)
            if self.buf and time.time() - self.last >= CFG["flush_interval"]:
                self.flush()


batch = Batch()


def broadcast(text: str, silent: bool = False):
    for cid in chats():
        tg.send(cid, text, silent=silent)


# ---------------------------------------------------------------- 權限確認
def on_permission_request(p: dict):
    rid = p.get("request_id", "")
    batch.flush()
    text = (f"🔐 [{project()}] Claude 請求確認：{p.get('tool_name', '?')}\n"
            f"{p.get('description', '')}\n\n{p.get('input_preview', '')[:1500]}\n\n"
            "💬 直接回覆文字 = 拒絕並改給指示")
    buttons = [[("✅ 允許", f"perm:{rid}:y"), ("❌ 拒絕", f"perm:{rid}:n")]]
    for cid in chats():
        res = tg.send(cid, text, buttons=buttons) or {}
        msg_id = (res.get("result") or {}).get("message_id")
        with lock:
            PENDING[rid] = {"chat": cid, "msg_id": msg_id, "text": text, "tool": p.get("tool_name")}


def close_pending(rid: str, mark: str):
    with lock:
        p = PENDING.pop(rid, None)
    if p and p["msg_id"]:
        tg.call("editMessageText", chat_id=p["chat"], message_id=p["msg_id"],
                text=(p["text"] + "\n\n" + mark)[:4096])


def verdict(rid: str, allow: bool) -> bool:
    if rid not in PENDING:
        return False
    mcp_notify("notifications/claude/channel/permission",
               {"request_id": rid, "behavior": "allow" if allow else "deny"})
    close_pending(rid, "→ ✅ 已允許（手機）" if allow else "→ ❌ 已拒絕（手機）")
    return True


def close_stale(tool: str = None):
    """電腦端已經處理過的確認：把手機上的按鈕收掉。"""
    for rid, p in list(PENDING.items()):
        if tool is None or p["tool"] == tool:
            close_pending(rid, "→ 已在電腦端處理")


# ---------------------------------------------------------------- hooks（HTTP）
def on_hook(ev: dict):
    name = ev.get("hook_event_name", "")
    log.info("hook %s %s", name, ev.get("tool_name") or "")
    if ev.get("agent_id"):  # 子代理內部的事件不轉播
        return
    state["cwd"] = ev.get("cwd") or state["cwd"]
    if name == "UserPromptSubmit":
        state["busy"] = True
        close_stale()
        prompt = (ev.get("prompt") or "").strip()
        if prompt in state["sent"]:
            state["sent"].discard(prompt)
        elif prompt and not prompt.startswith("<"):  # "<" 開頭是系統/channel 自動訊息
            batch.add(f"👤 電腦端：{prompt[:1500]}")
    elif name == "PostToolUse":
        close_stale(ev.get("tool_name"))
        tool = ev.get("tool_name")
        if CFG["show_tool_calls"] or tool == "TodoWrite":
            batch.add(describe_tool(tool, ev.get("tool_input")))
    elif name == "Notification":
        if ev.get("notification_type") not in ("idle_prompt", "permission_prompt"):
            batch.add(f"🔔 {ev.get('message', '')}")
    elif name == "Stop":
        state["busy"] = False
        close_stale()
        batch.flush()
        text = ((ev.get("last_assistant_message") or "").strip()
                or last_assistant_text(ev.get("transcript_path") or "") or "(無文字回覆)")
        broadcast(f"✅ [{project()}] 回合完成\n\n{text}")


class HookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            self.send_response(403)
            self.end_headers()
            return
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")  # 不做任何決定，馬上放行；推送在背景做
        try:
            HOOK_QUEUE.put(json.loads(body or b"{}"))
        except Exception:
            pass

    def log_message(self, *args):
        pass


HOOK_QUEUE: "queue.Queue[dict]" = queue.Queue()


def hook_worker():
    while True:  # 依序處理，確保手機上的訊息順序與 CLI 一致
        safe(on_hook, HOOK_QUEUE.get())


def safe(fn, *args):
    try:
        fn(*args)
    except Exception:
        log.exception("%s 失敗", fn.__name__)


# ---------------------------------------------------------------- Telegram 輸入
def authorized(chat_id, user_id) -> bool:
    if chat_id not in CFG["allowed_chat_ids"]:
        return False
    return not CFG["allowed_user_ids"] or user_id in CFG["allowed_user_ids"]


def push_to_claude(text: str, sender: str):
    state["sent"].add(text)
    mcp_notify("notifications/claude/channel", {"content": text, "meta": {"sender": sender}})


def on_text(chat_id, text: str, sender: str):
    m = PERM_REPLY_RE.match(text)
    if m and verdict(m.group(2).lower(), m.group(1).lower().startswith("y")):
        return
    if text.startswith("/"):
        cmd = text.split()[0].split("@")[0].lower()
        if cmd in ("/start", "/id", "/status"):
            tg.send(chat_id, f"🟢 已連線：{project()}\n📁 {state['cwd']}\n"
                             f"狀態：{'🔄 執行中' if state['busy'] else '💤 閒置'}"
                             f"\n待確認：{len(PENDING)} 筆", silent=True)
        else:
            tg.send(chat_id, "⚠️ 斜線指令（/clear、/compact…）無法從手機轉送，請在電腦端執行。", silent=True)
        return
    if PENDING:  # 有待確認時直接打字 = 拒絕並改給指示
        for rid in list(PENDING):
            verdict(rid, False)
        push_to_claude(f"I denied that action. Instead: {text}", sender)
        tg.send(chat_id, "↩️ 已拒絕，並把你的話轉告 Claude", silent=True)
        return
    push_to_claude(text, sender)
    tg.send(chat_id, "📨 已送出" + ("（Claude 執行中，下一輪處理）" if state["busy"] else ""),
            silent=True)


def on_update(u: dict):
    if "callback_query" in u:
        cq = u["callback_query"]
        chat_id = cq["message"]["chat"]["id"]
        if not authorized(chat_id, cq["from"]["id"]):
            tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="你沒有操作權限")
            return
        kind, rid, choice = (cq.get("data", "") + "::").split(":")[:3]
        ok = kind == "perm" and verdict(rid, choice == "y")
        tg.call("answerCallbackQuery", callback_query_id=cq["id"],
                **({} if ok else {"text": "此請求已失效"}))
        return
    msg = u.get("message") or u.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id, text = msg["chat"]["id"], msg["text"].strip()
    user = msg.get("from") or {}
    if not authorized(chat_id, user.get("id")):
        log.warning("未授權 chat=%s user=%s", chat_id, user.get("id"))
        if text.startswith(("/start", "/id")):
            tg.send(chat_id, f"chat id: {chat_id}\nuser id: {user.get('id')}\n"
                             "請加入 local/config.json 的 allowed_chat_ids / allowed_user_ids。")
        return
    on_text(chat_id, text, re.sub(r"\W", "_", user.get("username") or str(user.get("id"))))


def poll_loop():
    res = tg.call("getUpdates", offset=-1, timeout=0)  # 略過啟動前累積的舊訊息
    offset = (res.get("result") or [{}])[-1].get("update_id", -1) + 1 if res.get("ok") else None
    while True:
        res = tg.call("getUpdates", _timeout=60, _retry=0, offset=offset, timeout=50,
                      allowed_updates=["message", "callback_query"])
        if not res.get("ok"):
            if "Conflict" in str(res.get("description")):
                log.warning("另一個程式正在使用這個 bot（舊的 relay.py 或另一個 cctg？）")
            time.sleep(3)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            safe(on_update, u)


# ---------------------------------------------------------------- 進入點
def main():
    server = ThreadingHTTPServer(("127.0.0.1", PORT), HookHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    threading.Thread(target=batch.loop, daemon=True).start()
    threading.Thread(target=hook_worker, daemon=True).start()
    if CFG["bot_token"]:
        threading.Thread(target=poll_loop, daemon=True).start()
        tg.call("setMyCommands", commands=[{"command": "status", "description": "目前狀態"}])
        broadcast(f"🟢 [{project()}] CLI 已連線，直接打字就是對 Claude 說話", silent=True)
    else:
        log.error("local/config.json 沒有 bot_token")
    log.info("channel 啟動 port=%s cwd=%s", PORT, state["cwd"])
    mcp_loop()  # stdin 關閉 = CLI 結束
    batch.flush()
    broadcast(f"🔌 [{project()}] CLI 已結束", silent=True)


if __name__ == "__main__":
    main()
