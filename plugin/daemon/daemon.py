"""agent-proxy daemon：Claude Code hooks（HTTP）⇄ Telegram，每個 session 一張卡片。

- 卡片：我的輸入 + 當前進度，edit 原地更新；完成／需確認時刪卡重發（手機會響）
- 手機輸入：交給該 session 的 wait_phone（asyncRewake hook）叫醒 Claude；忙碌時排隊
- 權限：/away 時 PermissionRequest 等手機按鈕；/back（預設）時立即交給電腦
由 scripts/ensure_daemon.py 在 SessionStart 時拉起；固定 port 當單一實例鎖；
沒有 session 超過 idle_exit 秒自行結束。log：<資料夾>/daemon.log
"""
import json
import logging
import os
import queue
import secrets
import sys
import threading
import time
import urllib.parse
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
STATE_FILE = DATA_DIR / "state.json"  # {"away": bool, "target": sid, "cards": {sid: message_id}}
STATE = load_json(STATE_FILE, {})
STATE.setdefault("away", False)
STATE.setdefault("cards", {})
SEP = "━━━━━━━━━━━━"
VERSION = str(os.path.getmtime(__file__)) + "|" + os.path.abspath(__file__)  # 程式更新時 ensure_daemon 換掉舊的
PHONE_PREFIX = "The user sent this message from their phone (Telegram); treat it as their prompt:\n"
# Claude Code 自動送進來的輸入（背景任務／子代理回報、Stop hook 回饋、手機訊息本身等），不是我打的字
AUTO_PREFIXES = ("<", "Another Claude session", "[Subagent", PHONE_PREFIX[:30])
DONE_TASK = ("completed", "failed", "killed", "stopped", "done", "cancelled", "error")

lock = threading.RLock()
cond = threading.Condition(lock)  # 收件匣／狀態變化時喚醒 wait_phone 的長輪詢


def safe(fn, *args):
    try:
        return fn(*args)
    except Exception:
        log.exception("%s 失敗", getattr(fn, "__name__", fn))


def clip(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1] + "…"


def fmt_secs(s: float) -> str:
    s = int(s)
    return f"{s // 60}m{s % 60:02d}s" if s >= 60 else f"{s}s"


def save_state():
    STATE["cards"] = {sid: s.msg_id for sid, s in SESSIONS.items() if s.msg_id}
    save_json(STATE_FILE, STATE)


def describe_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name in ("Bash", "PowerShell"):
        text = f"💻 {inp.get('command', '')}"
        return text + (f"\n（{inp['description']}）" if inp.get("description") else "")
    if name in ("Edit", "MultiEdit", "Write", "NotebookEdit"):
        text = f"✏️ {name}: {inp.get('file_path') or inp.get('notebook_path', '')}"
        if name == "Edit":
            text += f"\n- {clip(inp.get('old_string', ''), 400)}\n+ {clip(inp.get('new_string', ''), 400)}"
        elif name == "Write":
            text += "\n" + clip(inp.get("content", ""), 600)
        return text
    if name == "WebFetch":
        return f"🌐 Fetch: {inp.get('url', '')}"
    if name == "ExitPlanMode":
        return "📝 計畫（批准後開始執行）：\n" + clip(inp.get("plan", ""), 2500)
    return f"🔧 {name}: {clip(json.dumps(inp, ensure_ascii=False), 600)}"


# ---------------------------------------------------------------- 權限 / 提問
class Pending:
    def __init__(self, sid, tool, inp, suggestions):
        self.rid, self.sid, self.tool, self.inp = secrets.token_hex(4), sid, tool, inp
        self.suggestions = suggestions or []
        self.questions = (inp.get("questions") or []) if tool == "AskUserQuestion" else []
        self.answers, self.qi = {}, 0
        self.result = None
        self.event = threading.Event()

    def resolve(self, result: dict):
        if not self.event.is_set():
            self.result = result
            self.event.set()

    def answer(self, text: str):
        self.answers[self.questions[self.qi].get("question", "")] = text
        self.qi += 1
        if self.qi >= len(self.questions):
            self.resolve({"behavior": "allow", "updatedInput": {**self.inp, "answers": self.answers}})

    def render(self) -> list:
        if self.questions:
            q = self.questions[self.qi]
            lines = [f"❓ {q.get('question', '')}"]
            lines += [f"{i + 1}. {o.get('label', '')} — {o.get('description', '')}"
                      for i, o in enumerate(q.get("options") or [])]
            return lines + ["💬 也可以直接回文字作答"]
        return [f"🔐 請求確認\n{clip(describe_tool(self.tool, self.inp), 2500)}",
                "💬 直接回文字 = 拒絕並改給指示"]

    def buttons(self) -> list:
        if self.questions:
            opts = self.questions[self.qi].get("options") or []
            return [[(clip(o.get("label", "?"), 40), f"ans:{self.rid}:{i}")] for i, o in enumerate(opts)]
        rows = [[("✅ 允許", f"perm:{self.rid}:y"), ("❌ 拒絕", f"perm:{self.rid}:n")]]
        if self.suggestions:
            rows.append([(f"✅ 之後都允許 {self.tool}", f"perm:{self.rid}:a")])
        return rows


# ---------------------------------------------------------------- Session 與卡片
class Session:
    def __init__(self, sid: str, cwd: str):
        self.sid, self.cwd = sid, cwd
        self.status = "idle"        # idle / sent / busy / done / ended
        self.prompt = ""
        self.started = self.finished = 0.0
        self.tools = 0
        self.todos: dict = {}       # id -> (status, text)
        self.reply = ""
        self.note = ""
        self.inbox: list = []       # 等著送進 Claude 的手機訊息
        self.waiter = None          # 目前在長輪詢的 wait_phone id
        self.polls = 0              # 進行中的長輪詢數；CLI 被直接關掉時歸零，用來清掉殘留 session
        self.poll_end = time.time()
        self.pending: Pending = None
        self.msg_id = STATE["cards"].get(sid)
        self.dirty = False
        self.last_edit = 0.0
        self.last_seen = time.time()
        self.truncated = False

    @property
    def project(self) -> str:
        return os.path.basename(self.cwd.rstrip("\\/")) or self.cwd or "?"

    @property
    def busy(self) -> bool:  # 太久沒有事件（例如 Esc 中斷沒有 Stop）就不再視為忙碌
        return self.status == "busy" and time.time() - self.last_seen < 600

    @property
    def label(self) -> str:
        return {"idle": "💤 閒置", "sent": "🕐 已送出", "busy": "🔄 執行中", "bg": "⏳ 背景執行中",
                "done": "✅ 完成", "ended": "⚫ 已結束"}[self.status]

    def start_turn(self, prompt: str):
        self.status, self.prompt, self.reply, self.note = "busy", prompt, "", ""
        self.started, self.finished, self.tools, self.todos = time.time(), 0.0, 0, {}

    def render(self) -> str:
        icon = self.label
        if self.pending:
            icon = "🔐 待確認"
        head = f"🟢 {self.project} · {icon}" + (" · away" if STATE["away"] else "")
        if len(SESSIONS) > 1 and target() is self:
            head = "🎯 " + head
        lines = [head]
        if self.prompt:
            lines += [SEP, f"👤 {clip(self.prompt, 600)}"]
        body = []
        if self.status == "sent":
            body.append("🕐 已送出，等待 Claude")
        elif self.started:
            elapsed = fmt_secs((self.finished or time.time()) - self.started)
            body.append(f"{'🔄' if self.status in ('busy', 'bg') else '⏱'} {elapsed} · 工具 {self.tools} 次")
        marks = {"completed": "✅", "in_progress": "🔄", "pending": "⬜"}
        body += [f"{marks.get(st, '•')} {clip(text, 120)}" for st, text in self.todos.values()]
        body += [f"⏳ 已排隊：{clip(t, 200)}" for t in self.inbox if self.status != "sent"]
        if self.note:
            body.append(self.note)
        if self.pending:
            body += ["", *self.pending.render()]
        if body:
            lines += [SEP, *body]
        text = clip("\n".join(lines), MAX_LEN)
        self.truncated = False
        if self.status == "done" and self.reply and not self.pending:
            room = MAX_LEN - len(text) - 2
            self.truncated = len(self.reply) > room
            text += "\n\n" + (clip(self.reply, room) if self.truncated else self.reply)
        return text

    def markup(self) -> dict:
        rows = self.pending.buttons() if self.pending else []
        if self.truncated:
            rows = [[("📄 看全文", f"full:{self.sid}")]]
        return {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in r] for r in rows]}


SESSIONS: dict = {}


def session(sid: str, cwd: str = "") -> Session:
    s = SESSIONS.get(sid)
    if not s:
        s = SESSIONS[sid] = Session(sid, cwd)
    if cwd:
        s.cwd = cwd
    s.last_seen = time.time()
    return s


def target():
    """手機訊息的目標：/to 指定的，否則最近有活動的 session。"""
    s = SESSIONS.get(STATE.get("target"))
    if s:
        return s
    return max(SESSIONS.values(), key=lambda x: x.last_seen, default=None)


def send_card(s: Session, notify: bool):
    """發新卡；有舊卡先刪除（刪除失敗不影響，例如已超過 48 小時）。"""
    if CHAT is None:
        return
    text, markup = s.render(), s.markup()
    if s.msg_id:
        tg.call("deleteMessage", chat_id=CHAT, message_id=s.msg_id)
    res = tg.call("sendMessage", chat_id=CHAT, text=text, disable_notification=not notify,
                  reply_markup=markup, link_preview_options={"is_disabled": True})
    s.msg_id = (res.get("result") or {}).get("message_id")
    s.dirty, s.last_edit = False, time.time()
    save_state()


def edit_card(s: Session):
    if CHAT is None:
        return
    if not s.msg_id:
        return send_card(s, notify=False)
    res = tg.call("editMessageText", chat_id=CHAT, message_id=s.msg_id, text=s.render(),
                  reply_markup=s.markup(), link_preview_options={"is_disabled": True})
    s.dirty, s.last_edit = False, time.time()
    if not res.get("ok") and "not modified" not in str(res.get("description")):
        s.msg_id = None  # 卡片被刪掉等 → 換一張新的
        send_card(s, notify=False)


def refresh(s: Session, now: bool = False, notify: bool = False):
    if notify:
        send_card(s, notify=True)
    elif now:
        edit_card(s)
    else:
        s.dirty = True  # 交給 ticker 依 edit_interval 節流


def refresh_all():
    for s in list(SESSIONS.values()):
        if s.msg_id:
            s.dirty = True


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
    log.info("hook %s %s %s", sid[:8], name,
             ev.get("tool_name") or repr((ev.get("prompt") or "")[:80]) if name == "UserPromptSubmit" or ev.get("tool_name") else "")
    if ev.get("agent_id"):  # 子代理內部事件不轉播
        return
    with cond:
        if sid not in SESSIONS and name != "SessionStart":  # 只追蹤註冊過的（互動）session
            return
        s = session(sid, ev.get("cwd") or "")
        if name == "UserPromptSubmit":
            prompt = (ev.get("prompt") or "").strip()
            if prompt and not prompt.startswith(AUTO_PREFIXES):  # 只有我實際打的字才算新的一回合
                s.start_turn(prompt)
                refresh(s, now=True)
            elif s.prompt and s.status != "sent":  # 自動接續（委派回報等）：沿用原本的輸入與計時
                s.status, s.finished = "busy", 0.0
                refresh(s)
        elif name == "PostToolUse" and s.status == "busy":
            s.tools += 1
            task_update(s, ev.get("tool_name") or "", ev.get("tool_input") or {}, ev.get("tool_response"))
            if s.note.startswith("🔐"):
                s.note = ""
            refresh(s)
        elif name == "Notification":
            if ev.get("notification_type") == "idle_prompt" and s.status == "busy":
                s.status = "done"
            elif ev.get("notification_type") not in ("idle_prompt", "permission_prompt") and s.prompt:
                s.note = f"🔔 {clip(ev.get('message', ''), 200)}"
                refresh(s)
        elif name == "Stop" and s.prompt:
            s.reply = (ev.get("last_assistant_message") or "").strip() or s.reply
            running = [t for t in ev.get("background_tasks") or []
                       if str(t.get("status", "running")).lower() not in DONE_TASK]
            log.info("stop %s background=%s", sid[:8],
                     [(t.get("type"), t.get("status"), clip(t.get("description", ""), 30)) for t in running])
            if running:  # 還有背景任務：回合其實沒結束，靜靜更新、不響（此時仍可接手機訊息）
                s.status, s.note = "bg", f"⏳ 背景任務 {len(running)} 個執行中"
                refresh(s, now=True)
            else:        # 全部做完才算完成，重發卡片讓手機響一次
                s.status, s.finished, s.note = "done", time.time(), ""
                refresh(s, notify=CFG["notify_on_done"] and not s.inbox, now=True)
        elif name == "SessionEnd":
            s.status, s.finished = "ended", s.finished or time.time()
            if s.msg_id:
                refresh(s, now=True)
            SESSIONS.pop(sid, None)
            if STATE.get("target") == sid:
                STATE.pop("target")
            save_state()
        cond.notify_all()


def on_permission(ev: dict) -> dict:
    """PermissionRequest：back 時立即交給電腦；away 時等手機決定（逾時退回電腦）。"""
    tool, inp = ev.get("tool_name") or "?", ev.get("tool_input") or {}
    with cond:
        if ev.get("session_id") not in SESSIONS:  # 未註冊（例如 claude -p）→ 照常交給 CLI
            return {}
        s = session(ev.get("session_id"), ev.get("cwd") or "")
        if not STATE["away"]:
            s.note = f"🔐 電腦端等待確認：{clip(describe_tool(tool, inp), 150)}"
            refresh(s, now=True)
            return {}
        p = s.pending = Pending(s.sid, tool, inp, ev.get("permission_suggestions"))
        log.info("權限請求 %s rid=%s tool=%s", s.sid[:8], p.rid, tool)
        refresh(s, notify=True)
    ok = p.event.wait(CFG["away_timeout"])
    with cond:
        s.pending = None
        if not ok:
            s.note = "⌛ 手機未回應，改由電腦端確認"
            refresh(s, now=True)
            return {}
        res = p.result
        if res.get("behavior") == "allow":
            dec = {"behavior": "allow"}
            if "answers" in (res.get("updatedInput") or {}):
                dec["updatedInput"] = res["updatedInput"]
                s.note = "💬 已回答：" + "、".join(p.answers.values())
            else:
                s.note = "✅ 已允許" + (f"（之後都允許 {tool}）" if res.get("updatedPermissions") else "")
            if res.get("updatedPermissions"):
                dec["updatedPermissions"] = res["updatedPermissions"]
        else:
            dec = {"behavior": "deny", "message": res.get("message", "")}
            s.note = "↩️ 已拒絕並改給指示" if res.get("said") else "❌ 已拒絕"
        refresh(s, now=True)
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}}


def wait_phone(sid: str, wid: str, cwd: str, timeout: float = 50) -> dict:
    """wait_phone 的長輪詢：有手機訊息且 session 不忙時交給它；新的 waiter 會取代舊的。"""
    end = time.time() + timeout
    with cond:
        s = session(sid, cwd)
        if s.waiter != wid:
            s.waiter = wid
            cond.notify_all()
        s.polls += 1
        try:
            return _wait_loop(s, sid, wid, end)
        finally:
            s.polls -= 1
            s.poll_end = time.time()


def _wait_loop(s: Session, sid: str, wid: str, end: float) -> dict:
    with cond:
        while True:
            if s.waiter != wid or sid not in SESSIONS:
                return {"exit": True}
            if s.inbox and not s.busy and not s.pending:
                text = s.inbox.pop(0)
                s.start_turn(text)
                threading.Thread(target=safe, args=(edit_card, s), daemon=True).start()
                log.info("送出手機訊息 %s", sid[:8])
                return {"text": PHONE_PREFIX + text}
            left = end - time.time()
            if left <= 0:
                return {"timeout": True}
            cond.wait(left)
            s.last_seen = time.time()


HOOK_QUEUE: "queue.Queue[dict]" = queue.Queue()


def hook_worker():
    while True:  # 依序處理，卡片內容與 CLI 一致
        safe(on_hook, HOOK_QUEUE.get())


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        q = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == "/wait":
            return self.reply(wait_phone(q.get("sid", "?"), q.get("wid", ""), q.get("cwd", "")))
        if url.path == "/quit":  # ensure_daemon 發現版本不符時要求退出
            self.reply({"ok": True})
            log.info("收到 /quit，daemon 結束（版本更新）")
            threading.Thread(target=lambda: (time.sleep(0.2), os._exit(0)), daemon=True).start()
            return
        self.reply({"ok": True, "pid": os.getpid(), "version": VERSION})

    def do_POST(self):
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        try:
            ev = json.loads(body or b"{}")
        except Exception:
            return self.reply({})
        if self.path == "/debug/update" and os.environ.get("AP_DEBUG"):  # 測試用：模擬 Telegram 更新
            safe(on_update, ev)
            return self.reply({})
        if ev.get("hook_event_name") == "PermissionRequest":
            return self.reply(safe(on_permission, ev) or {})
        self.reply({})  # 不做決定、立即放行；處理在背景佇列
        HOOK_QUEUE.put(ev)

    def reply(self, obj: dict):
        data = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


# ---------------------------------------------------------------- Telegram 輸入
def authorized(chat_id, user_id) -> bool:
    return chat_id in CFG["allowed_chat_ids"] and (
        not CFG["allowed_user_ids"] or user_id in CFG["allowed_user_ids"])


def notice(chat_id, text: str, ttl: float = 15, buttons=None):
    """暫時性訊息（指令回覆等），ttl 秒後自動刪除，聊天室保持只有卡片。"""
    kw = {"reply_markup": {"inline_keyboard": [[{"text": t, "callback_data": d} for t, d in r]
                                               for r in buttons]}} if buttons else {}
    res = tg.call("sendMessage", chat_id=chat_id, text=text, disable_notification=True, **kw)
    mid = (res.get("result") or {}).get("message_id")
    if mid:
        t = threading.Timer(ttl, lambda: tg.call("deleteMessage", chat_id=chat_id, message_id=mid))
        t.daemon = True
        t.start()


def send_long(chat_id, text: str):
    while text:
        cut = len(text)
        if cut > MAX_LEN:
            nl = text.rfind("\n", 0, MAX_LEN)
            cut = nl if nl > MAX_LEN // 2 else MAX_LEN
        tg.call("sendMessage", chat_id=chat_id, text=text[:cut], link_preview_options={"is_disabled": True})
        text = text[cut:].lstrip("\n")


def find_pending(rid: str):
    for s in SESSIONS.values():
        if s.pending and s.pending.rid == rid:
            return s.pending
    return None


def on_callback(cq: dict):
    chat_id = cq["message"]["chat"]["id"]
    if not authorized(chat_id, cq["from"]["id"]):
        return tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="你沒有操作權限")
    kind, _, val = cq.get("data", "").partition(":")
    msg = ""
    with cond:
        if kind in ("perm", "ans"):
            rid, _, choice = val.partition(":")
            p = find_pending(rid)
            if not p or p.event.is_set():
                msg = "此請求已失效"
            elif kind == "ans":
                p.answer((p.questions[p.qi].get("options") or [])[int(choice)].get("label", ""))
                if not p.event.is_set():
                    refresh(SESSIONS[p.sid], now=True)  # 下一題
            elif choice == "n":
                p.resolve({"behavior": "deny", "message": "The user denied this action."})
            else:
                res = {"behavior": "allow", "updatedInput": p.inp}
                if choice == "a":
                    res["updatedPermissions"] = p.suggestions
                p.resolve(res)
        elif kind == "full":
            s = SESSIONS.get(val)
            if s and s.reply:
                send_long(chat_id, f"📄 {s.project} 完整回覆\n\n{s.reply}")
            else:
                msg = "內容已不存在"
        elif kind == "to":
            if val in SESSIONS:
                STATE["target"] = val
                save_state()
                refresh_all()
                msg = f"🎯 目標：{SESSIONS[val].project}"
            else:
                msg = "這個 session 已結束"
    tg.call("answerCallbackQuery", callback_query_id=cq["id"], **({"text": msg} if msg else {}))


def on_command(chat_id, text: str):
    cmd = text.split()[0].split("@")[0].lower()
    with cond:
        if cmd in ("/away", "/back"):
            STATE["away"] = cmd == "/away"
            save_state()
            refresh_all()
            return notice(chat_id, "🚶 away：權限確認改到手機" if STATE["away"]
                          else "🪑 back：權限確認在電腦端")
        if cmd == "/to":
            rows = [[(("🎯 " if s is target() else "") + f"{s.project} · {s.label}", f"to:{s.sid}")]
                    for s in SESSIONS.values()]
            return notice(chat_id, "選擇手機訊息要送到哪個 session：" if rows else "沒有開著的 session",
                          ttl=60, buttons=rows)
        if cmd in ("/status", "/start", "/id"):
            t = target()
            rows = [f"{'🎯' if s is t else '•'} {s.project} · {s.label}" for s in SESSIONS.values()]
            return notice(chat_id, f"{'🚶 away' if STATE['away'] else '🪑 back'}\n"
                          + ("\n".join(rows) or "沒有開著的 session"), ttl=30)
    notice(chat_id, "⚠️ 斜線指令（/clear、/compact…）無法從手機送出，請在電腦端執行")


def on_text(chat_id, text: str):
    with cond:
        s = target()
        if not s:
            return notice(chat_id, "沒有開著的 session（插件安裝後開的 session 才會出現）")
        p = s.pending
        if p and p.questions:
            p.answer(text)
            if not p.event.is_set():
                refresh(s, now=True)
        elif p:
            p.resolve({"behavior": "deny", "said": True,
                       "message": f"The user denied this action and said: {text}"})
        else:
            s.inbox.append(text)
            if not s.busy:
                s.status, s.prompt, s.note = "sent", text, ""
                s.started, s.todos = 0.0, {}
            if not s.waiter:
                s.note = "⚠️ 這個 session 沒有在等手機訊息（插件安裝前就開著？）"
            refresh(s, now=True)
            cond.notify_all()


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
                    text=f"chat id: {chat_id}\nuser id: {user_id}\n請加入設定的 allowed_chat_ids / allowed_user_ids")
        return
    tg.call("deleteMessage", chat_id=chat_id, message_id=msg["message_id"])  # 聊天室只留卡片
    if msg["chat"]["type"] != "private" and text.startswith("@"):
        text = text.partition(" ")[2].strip()
    if text.startswith("/"):
        on_command(chat_id, text)
    elif text:
        on_text(chat_id, text)


def poll_loop():
    res = tg.call("getUpdates", offset=-1, timeout=0)  # 略過啟動前累積的舊訊息
    offset = ((res.get("result") or [{}])[-1].get("update_id", -1) + 1) if res.get("ok") else None
    while True:
        res = tg.call("getUpdates", _timeout=60, _retry=0, offset=offset, timeout=50,
                      allowed_updates=["message", "callback_query"])
        if not res.get("ok"):
            if "Conflict" in str(res.get("description")):
                log.warning("另一個程式正在使用這個 bot")
            time.sleep(3)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            safe(on_update, u)


# ---------------------------------------------------------------- 生命週期
def reaper():
    """清掉殘留 session（waiter 停止輪詢超過 2 分鐘，或 6 小時沒動靜）；沒有 session 超過 idle_exit 秒就結束。"""
    empty_since = time.time()
    while True:
        time.sleep(30)
        with cond:
            for sid, s in list(SESSIONS.items()):
                gone = s.waiter and not s.polls and time.time() - s.poll_end > 120
                if gone or time.time() - s.last_seen > 6 * 3600:
                    log.info("移除殘留 session %s", sid[:8])
                    if s.msg_id and gone:
                        s.status, s.finished = "ended", s.finished or time.time()
                        safe(edit_card, s)
                    SESSIONS.pop(sid)
                    if STATE.get("target") == sid:
                        STATE.pop("target")
                    save_state()
            if SESSIONS:
                empty_since = time.time()
            cond.notify_all()
        if time.time() - empty_since > CFG["idle_exit"]:
            log.info("沒有 session，daemon 結束")
            os._exit(0)


def main():
    try:
        server = ThreadingHTTPServer(("127.0.0.1", CFG["port"]), Handler)
    except OSError:
        log.info("port %s 已被佔用，daemon 已在執行", CFG["port"])
        return
    server.daemon_threads = True
    if not CFG["bot_token"] or CHAT is None:
        log.error("缺少 bot_token 或 allowed_chat_ids（插件設定或 %s）", DATA_DIR / "config.json")
    else:
        threading.Thread(target=poll_loop, daemon=True).start()
        tg.call("setMyCommands", commands=[
            {"command": c, "description": d} for c, d in (
                ("status", "目前狀態"), ("to", "選擇手機訊息的目標 session"),
                ("away", "離開：權限確認改到手機"), ("back", "回來：權限確認在電腦端"))])
    for fn in (ticker, hook_worker, reaper):
        threading.Thread(target=fn, daemon=True).start()
    log.info("daemon 啟動 pid=%s port=%s", os.getpid(), CFG["port"])
    server.serve_forever()


if __name__ == "__main__":
    main()
