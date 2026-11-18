"""Telegram <-> Claude Code 中繼程式。

只接管電腦上已經開著的互動式 Claude CLI（透過 cli_hook.py 登記）；選定後即時轉播它的
輸入、工具呼叫、回覆，手機傳的文字（含 /clear 等 CLI 指令）直接打進該 CLI，授權請求推到手機。
relay 本身不會啟動任何 claude 行程。
"""
import ctypes
import json
import logging
import os
import pathlib
import re
import secrets
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tgapi import (TG, BASE, LOCAL_DIR, RUNTIME, RELAY_INFO, last_assistant_text, load_config,
                   load_json, save_json)

for _s in (sys.stdout, sys.stderr):  # Windows 主控台 (cp950) 無法輸出 emoji
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("relay")

CFG = load_config()
STATE_FILE = LOCAL_DIR / "state.json"
INJECT = BASE / "console_inject.py"
TOKEN = secrets.token_hex(16)
URL_BASE = ""  # main() 啟動本機 HTTP 伺服器後填入
DETACHED_PROCESS = 0x00000008
tg = TG(CFG["bot_token"])

state_lock = threading.Lock()
STATE = load_json(STATE_FILE, {})


def chat_state(chat_id) -> dict:
    with state_lock:
        st = STATE.setdefault(str(chat_id), {})
        for k in ("cwd", "session_id", "mode", "allowed_tools"):  # 舊版背景模式留下的欄位
            st.pop(k, None)
        st.setdefault("target_pid", None)  # 正在接管的 CLI；None = 未接管
        return st


def save_state():
    with state_lock:
        save_json(STATE_FILE, STATE)


# ---------------------------------------------------------------- 工具描述
def describe_tool(name: str, inp: dict) -> str:
    inp = inp or {}
    if name == "Bash":
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
        lines = [f"{marks.get(t.get('status'), '•')} {t.get('content', '')}"
                 for t in inp.get("todos", [])]
        return "📋 進度\n" + "\n".join(lines)
    return f"🔧 {name}: {json.dumps(inp, ensure_ascii=False)[:300]}"


# ---------------------------------------------------------------- 批次回報（避免洗版與限流）
class Reporter:
    def __init__(self, chat_id):
        self.chat_id, self.buf, self.last = chat_id, [], 0.0
        self.lock = threading.Lock()

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
            tg.send(self.chat_id, "\n\n".join(buf), silent=True)

    def tick(self):
        if self.buf and time.time() - self.last >= CFG["flush_interval"]:
            self.flush()


REPORTERS: dict = {}


def reporter(chat_id) -> Reporter:
    if chat_id not in REPORTERS:
        REPORTERS[chat_id] = Reporter(chat_id)
    return REPORTERS[chat_id]


def _flusher():
    while True:
        time.sleep(1)
        for r in list(REPORTERS.values()):
            try:
                r.tick()
            except Exception:
                log.exception("flush 失敗")


# ---------------------------------------------------------------- 授權 / 回答問題
class Pending:
    def __init__(self, chat_id, tool, inp, src, suggestions=None):
        self.chat_id, self.tool, self.inp, self.src = chat_id, tool, inp, src
        self.suggestions = suggestions or []  # CLI 模式：「之後都允許」要套用的權限規則
        self.event = threading.Event()
        self.result = None
        self.questions = (inp.get("questions") or []) if tool == "AskUserQuestion" else []
        self.answers: dict = {}
        self.qi = 0

    def resolve(self, result):
        if not self.event.is_set():
            self.result = result
            self.event.set()


PENDING: dict = {}  # id -> Pending


def allow(inp):
    return {"behavior": "allow", "updatedInput": inp}


def preview(tool: str, inp: dict) -> str:
    if tool == "ExitPlanMode":
        return "📝 Claude 的計畫，批准後開始執行：\n\n" + (inp.get("plan") or "")[:3000]
    text = describe_tool(tool, inp)
    if tool == "Edit":
        text += f"\n- {inp.get('old_string', '')[:600]}\n+ {inp.get('new_string', '')[:600]}"
    elif tool == "Write":
        text += "\n" + (inp.get("content") or "")[:800]
    elif tool == "Bash" and inp.get("description"):
        text += f"\n（{inp['description']}）"
    return text


def ask_question(pid: str):
    p = PENDING[pid]
    q = p.questions[p.qi]
    opts = q.get("options") or []
    lines = [f"❓ {q.get('question', '')}"]
    lines += [f"{i + 1}. {o.get('label', '')} — {o.get('description', '')}" for i, o in enumerate(opts)]
    lines.append("\n💬 也可以直接回覆文字作答" + ("（複選請用逗號分隔）" if q.get("multiSelect") else ""))
    tg.send(p.chat_id, "\n".join(lines),
            buttons=[[(o.get("label", "?")[:40], f"ans:{pid}:{i}")] for i, o in enumerate(opts)])


def answer_question(pid: str, answer: str):
    p = PENDING[pid]
    p.answers[p.questions[p.qi].get("question", "")] = answer
    p.qi += 1
    if p.qi < len(p.questions):
        ask_question(pid)
    else:
        p.resolve(allow({**p.inp, "answers": p.answers}))


def ask_phone(p: Pending, timeout: float, header: str = ""):
    """推播授權請求並等待；回傳 Pending.result，逾時回傳 None。"""
    pid = secrets.token_hex(4)
    PENDING[pid] = p
    reporter(p.chat_id).flush()
    try:
        if p.questions:
            ask_question(pid)
        else:
            rows = [[("✅ 允許", f"perm:{pid}:y"), ("❌ 拒絕", f"perm:{pid}:n")]]
            if p.suggestions:
                rows.append([(f"✅ 之後都允許 {p.tool}", f"perm:{pid}:a")])
            tg.send(p.chat_id, f"🔐 {header}Claude 請求確認\n\n{preview(p.tool, p.inp)}\n\n"
                               "💬 直接回覆文字 = 拒絕並告訴 Claude 改怎麼做", buttons=rows)
        return p.result if p.event.wait(timeout) else None
    finally:
        PENDING.pop(pid, None)


def pending_for(chat_id):
    for pid, p in list(PENDING.items()):
        if p.chat_id == chat_id and not p.event.is_set():
            return pid, p
    return None, None


# ---------------------------------------------------------------- 互動式 CLI 登記與接管
CLIS: dict = {}  # pid -> {pid, cwd, session_id, transcript, status, ts, title, injected}
cli_lock = threading.Lock()


def pid_alive(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    k32 = ctypes.windll.kernel32
    h = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE | QUERY_LIMITED_INFORMATION
    if not h:
        return False
    alive = k32.WaitForSingleObject(h, 0) == 0x102  # WAIT_TIMEOUT
    k32.CloseHandle(h)
    return alive


def config_dirs():
    """所有可能的 Claude 設定目錄：CLAUDE_CONFIG_DIR、~/.claude、~/.claude-*、config 的 claude_config_dirs。"""
    home = pathlib.Path.home()
    dirs = [os.environ.get("CLAUDE_CONFIG_DIR"), home / ".claude", *home.glob(".claude-*"),
            *CFG.get("claude_config_dirs", [])]
    seen, out = set(), []
    for d in dirs:
        if not d:
            continue
        d = pathlib.Path(d)
        key = str(d.resolve()).lower()
        if key not in seen and (d / "sessions").is_dir():
            seen.add(key)
            out.append(d)
    return out


def hook_installed(d: pathlib.Path) -> bool:
    try:
        return "cli_hook.py" in (d / "settings.json").read_text(encoding="utf-8")
    except Exception:
        return False


def scan_sessions():
    """讀 <設定目錄>/sessions/<pid>.json，把還沒觸發過 hook 的互動式 CLI 也列進來。"""
    for d in config_dirs():
        hooked = hook_installed(d)
        for f in (d / "sessions").glob("*.json"):
            try:
                info = json.loads(f.read_text(encoding="utf-8"))
                pid = int(info["pid"])
            except Exception:
                continue
            if info.get("kind") != "interactive" or not pid_alive(pid):
                continue
            cwd, sid = info.get("cwd") or "?", info.get("sessionId")
            updated = (info.get("updatedAt") or 0) / 1000
            with cli_lock:
                c = CLIS.setdefault(pid, {"pid": pid, "status": "idle", "title": "",
                                          "injected": None, "ts": updated})
                if c.get("reported"):
                    continue  # hook 回報的資訊比較即時（含等待確認狀態）
                c.update(cwd=cwd, session_id=sid, config_dir=str(d), ts=max(c["ts"], updated), hooked=hooked,
                         status="busy" if info.get("status") == "busy" else "idle",
                         transcript=str(d / "projects" / re.sub(r"[^A-Za-z0-9]", "-", cwd)
                                        / f"{sid}.jsonl"))


def live_clis():
    try:
        scan_sessions()
    except Exception:
        log.exception("掃描 sessions 失敗")
    with cli_lock:
        for pid in [p for p in CLIS if not pid_alive(p)]:
            CLIS.pop(pid)
        return sorted(CLIS.values(), key=lambda c: c["ts"], reverse=True)


def cli_label(c) -> str:
    icon = {"idle": "💤", "busy": "🔄", "waiting": "🔐"}.get(c["status"], "•")
    warn = "" if c.get("hooked", True) else " ⚠️"
    return f"{icon} {pathlib.Path(c['cwd']).name or c['cwd']} · {c['pid']}{warn}"


def watchers(pid):
    return [int(cid) for cid, st in list(STATE.items()) if st.get("target_pid") == pid]


def unwatch_all(pid, reason):
    for cid in watchers(pid):
        chat_state(cid)["target_pid"] = None
        tg.send(cid, f"{reason}\n已放開，/cli 可重新選擇")
    save_state()


def inject(pid, *args) -> str:
    """打字進 CLI 的主控台；成功回傳空字串，失敗回傳原因。"""
    try:
        r = subprocess.run([sys.executable, str(INJECT), str(pid), *args],
                           creationflags=DETACHED_PROCESS if os.name == "nt" else 0,
                           capture_output=True, text=True, timeout=15)
    except Exception as e:
        return str(e)
    return "" if r.returncode == 0 else (r.stderr.strip() or f"exit {r.returncode}")


def send_to_cli(chat_id, text: str):
    pid = chat_state(chat_id)["target_pid"]
    c = CLIS.get(pid)
    if not c or not pid_alive(pid):
        unwatch_all(pid, "🔌 這個 CLI 已經關閉")
        return
    if c["status"] == "waiting":
        tg.send(chat_id, "⚠️ CLI 正在電腦端等待確認視窗，現在打字會打進確認視窗。\n"
                         "請到電腦處理，或用 /esc 取消後再傳。")
        return
    c["injected"] = text.strip()
    err = inject(pid, text)
    if err:
        tg.send(chat_id, f"❌ 無法輸入到這個 CLI：{err}\n"
                         "（VSCode 擴充面板版沒有主控台，只能監控與授權；終端機版才能打字）")
        return
    note = "（CLI 執行中，會排入它的佇列）" if c["status"] == "busy" else ""
    tg.send(chat_id, f"📨 已送出{note}", silent=True)


def handle_hook(ev: dict) -> dict:
    pid = ev.get("cli_pid")
    if not pid:
        return {}
    name = ev.get("hook_event_name", "")
    now = time.time()
    with cli_lock:
        c = CLIS.setdefault(pid, {"pid": pid, "status": "idle", "title": "", "injected": None})
        c.update(ts=now, hooked=True, reported=True, cwd=ev.get("cwd") or c.get("cwd", "?"),
                 session_id=ev.get("session_id") or c.get("session_id"),
                 transcript=ev.get("transcript_path") or c.get("transcript"))
    chats = watchers(pid)
    proj = pathlib.Path(c["cwd"]).name

    if name == "SessionEnd":
        # /clear、/resume 也會觸發 SessionEnd，但 CLI 還在；等幾秒確認行程真的結束
        def check():
            time.sleep(4)
            if not pid_alive(pid):
                with cli_lock:
                    CLIS.pop(pid, None)
                if watchers(pid):
                    unwatch_all(pid, f"🔌 [{proj}] CLI 已結束")
        threading.Thread(target=check, daemon=True).start()
        return {}
    if name == "SessionStart":
        c["status"] = "idle"
        msg = {"clear": "🧹 已清除對話（/clear）", "compact": "🗜️ 對話已壓縮"}.get(ev.get("source"))
        if msg:
            c["title"] = ""
            for cid in chats:
                reporter(cid).add(msg)
    elif name == "UserPromptSubmit":
        c["status"] = "busy"
        prompt = (ev.get("prompt") or "").strip()
        c["title"] = c["title"] or prompt[:30]
        if prompt and prompt != c.get("injected"):
            for cid in chats:
                reporter(cid).add(f"👤 電腦端輸入：{prompt[:1500]}")
        c["injected"] = None
    elif name == "PostToolUse":
        if c["status"] == "waiting":
            c["status"] = "busy"
        if CFG["show_tool_calls"] and ev.get("tool_name") != "AskUserQuestion":
            for cid in chats:
                reporter(cid).add(describe_tool(ev.get("tool_name"), ev.get("tool_input")))
    elif name == "Notification":
        for cid in chats:
            reporter(cid).add(f"🔔 {ev.get('message', '')}")
    elif name == "Stop":
        c["status"] = "idle"
        if chats:
            text = ((ev.get("last_assistant_message") or "").strip()
                    or last_assistant_text(c.get("transcript") or "") or "(無文字回覆)")
            for cid in chats:
                reporter(cid).flush()
                tg.send(cid, f"✅ [{proj}] 回合完成\n\n{text}")
    elif name == "PermissionRequest" and chats:
        c["status"] = "waiting"
        p = Pending(chats[0], ev.get("tool_name", "?"), ev.get("tool_input") or {}, "cli",
                    ev.get("permission_suggestions"))
        res = ask_phone(p, CFG["cli_approval_timeout"], f"[{proj}] ")
        if res is None:
            tg.send(chats[0], "⌛ 手機未回應，改由電腦端確認")
            return {}  # 不回傳決定 -> CLI 照常在電腦上顯示確認視窗
        c["status"] = "busy"
        if res.get("behavior") == "allow":
            dec = {"behavior": "allow"}
            if "answers" in (res.get("updatedInput") or {}):
                dec["updatedInput"] = res["updatedInput"]
            if res.get("updatedPermissions"):
                dec["updatedPermissions"] = res["updatedPermissions"]
        else:
            dec = {"behavior": "deny", "message": res.get("message", "")}
            if res.get("interrupt"):
                dec["interrupt"] = True
        return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}}
    return {}


def watch(chat_id, pid: int):
    st = chat_state(chat_id)
    if not pid:
        st["target_pid"] = None
        save_state()
        tg.send(chat_id, "🔓 已放開 CLI。手機訊息不會送到任何地方，/cli 可重新接管")
        return
    c = CLIS.get(pid)
    if not c or not pid_alive(pid):
        tg.send(chat_id, "這個 CLI 已經不在了，/cli 重新整理")
        return
    st["target_pid"] = pid
    save_state()
    if not c.get("hooked", True):
        tg.send(chat_id, "⚠️ 這個 CLI 還沒回報過 hook 事件：可以打字進去，但收不到回覆與授權請求。\n"
                         f"請確認 {c.get('config_dir', '它的設定目錄')} 的 settings.json 已安裝 hook"
                         "（python install_hook.py <settings路徑>），CLI 下次有動作就會自動補上。")
    last = last_assistant_text(c.get("transcript") or "", 1500)
    tg.send(chat_id, f"📡 已接管 {cli_label(c)}\n📁 {c['cwd']}\n🧵 {c.get('session_id')}\n\n"
                     f"之後傳的文字會直接打進這個 CLI；/clear /compact 等指令也會轉送。\n"
                     f"/esc 中斷它，/cli 切換，/bg 放開。"
                     + (f"\n\n— 最後回覆 —\n{last}" if last else ""))


# ---------------------------------------------------------------- 本機 HTTP（hook 呼叫）
class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.headers.get("X-Token") != TOKEN:
            self.send_response(403)
            self.end_headers()
            return
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            res = handle_hook(req)
        except Exception:
            log.exception("處理 %s 失敗", self.path)
            res = {}
        data = json.dumps(res, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


def start_local_server():
    global URL_BASE
    srv = ThreadingHTTPServer(("127.0.0.1", CFG.get("local_port", 0)), Handler)  # 只聽本機
    srv.daemon_threads = True
    URL_BASE = f"http://127.0.0.1:{srv.server_address[1]}"
    RUNTIME.mkdir(exist_ok=True)
    save_json(RELAY_INFO, {"url": URL_BASE + "/hook", "token": TOKEN})
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log.info("本機端點 %s", URL_BASE)


# ---------------------------------------------------------------- Telegram 指令
HELP = """🤖 Claude Code 中繼（只接管電腦上已開著的 CLI，不會自己啟動 claude）

/cli 選擇要接管的 CLI（需先安裝 hook）
接管後：直接打字 = 打進該 CLI；/clear /compact /model 等 CLI 指令也會轉送
/esc 中斷 CLI 目前的動作
/bg 放開 CLI
/status 目前狀態 · /id 顯示 id
Claude 請求確認時會推播按鈕；此時直接回覆文字 = 拒絕並給它新指示，或回答它的問題"""

RELAY_CMDS = {"/start", "/help", "/id", "/status", "/cli", "/bg", "/esc"}
NO_TARGET = "目前沒有接管 CLI，請用 /cli 選擇（relay 不會自己啟動 claude）"


def handle_command(chat_id, text: str):
    cmd = text.partition(" ")[0].split("@")[0].lower()
    target = chat_state(chat_id)["target_pid"]

    if cmd not in RELAY_CMDS:  # 其他 / 指令原樣轉給 CLI（/clear、/compact…）
        if target:
            send_to_cli(chat_id, text)
        else:
            tg.send(chat_id, NO_TARGET)
        return

    if cmd in ("/start", "/help"):
        tg.send(chat_id, HELP)
    elif cmd == "/id":
        tg.send(chat_id, f"chat id: {chat_id}")
    elif cmd == "/cli":
        clis = live_clis()
        rows = [[(("📡 " if c["pid"] == target else "") + cli_label(c)
                  + (f" · {c['title']}" if c["title"] else ""), f"watch:{c['pid']}")] for c in clis]
        if target:
            rows.append([("🔓 放開目前的 CLI", "watch:0")])
        msg = "選擇要接管的 CLI：" if clis else "目前沒有偵測到開著的互動式 CLI。"
        if any(not c.get("hooked", True) for c in clis):
            msg += "\n⚠️ = 尚未收到 hook 回報（只能打字、看不到回覆）"
        tg.send(chat_id, msg, buttons=rows or None)
    elif cmd == "/bg":
        watch(chat_id, 0)
    elif cmd == "/esc":
        if not target:
            tg.send(chat_id, NO_TARGET)
            return
        err = inject(target, "--keys", "esc")
        tg.send(chat_id, f"❌ {err}" if err else "⎋ 已送出 Esc", silent=True)
    elif cmd == "/status":
        if target and target in CLIS:
            c = CLIS[target]
            tg.send(chat_id, f"📡 接管中：{cli_label(c)}\n📁 {c['cwd']}\n🧵 {c.get('session_id')}")
        else:
            tg.send(chat_id, f"🔓 未接管 CLI\n🖥️ 已登記 CLI：{len(live_clis())} 個（/cli）")


def handle_callback(cq):
    chat_id = cq["message"]["chat"]["id"]
    kind, _, val = cq.get("data", "").partition(":")

    if kind in ("perm", "ans"):
        pid, _, choice = val.partition(":")
        p = PENDING.get(pid)
        if not p or p.event.is_set():
            tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="此請求已失效")
            return
        tg.call("answerCallbackQuery", callback_query_id=cq["id"])
        if kind == "ans":
            label = (p.questions[p.qi].get("options") or [])[int(choice)].get("label", "")
            mark = f"→ {label}"
            answer_question(pid, label)
        elif choice == "n":
            mark = "→ ❌ 已拒絕"
            p.resolve({"behavior": "deny", "message": "The user denied this action."})
        else:
            mark = "→ ✅ 已允許"
            res = allow(p.inp)
            if choice == "a":
                mark = f"→ ✅ 已允許（之後都允許 {p.tool}）"
                res["updatedPermissions"] = p.suggestions
            p.resolve(res)
        tg.call("editMessageText", chat_id=chat_id, message_id=cq["message"]["message_id"],
                text=(cq["message"].get("text", "") + "\n\n" + mark)[:4096])
        return

    tg.call("answerCallbackQuery", callback_query_id=cq["id"])
    if kind == "watch":
        watch(chat_id, int(val))


def authorized(chat_id, user_id) -> bool:
    if chat_id not in CFG["allowed_chat_ids"]:
        return False
    return not CFG["allowed_user_ids"] or user_id in CFG["allowed_user_ids"]


def handle_update(u):
    if "callback_query" in u:
        cq = u["callback_query"]
        if authorized(cq["message"]["chat"]["id"], cq["from"]["id"]):
            handle_callback(cq)
        else:
            tg.call("answerCallbackQuery", callback_query_id=cq["id"], text="你沒有操作權限")
        return
    msg = u.get("message") or u.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat_id, text = msg["chat"]["id"], msg["text"].strip()
    user_id = (msg.get("from") or {}).get("id")
    if not authorized(chat_id, user_id):
        log.warning("未授權 chat=%s user=%s: %s", chat_id, user_id, text[:50])
        if text.startswith(("/start", "/id")):
            tg.send(chat_id, f"chat id: {chat_id}\nuser id: {user_id}\n"
                             "請加入 local/config.json 的 allowed_chat_ids / allowed_user_ids 後重啟中繼。")
        return
    if msg["chat"]["type"] != "private" and text.startswith("@"):
        text = text.partition(" ")[2].strip()  # 群組裡 "@bot 指令" -> 去掉提及

    pid, p = pending_for(chat_id)
    if p and not text.startswith("/"):
        if p.questions:            # 回答 Claude 的問題
            answer_question(pid, text)
        else:                      # 拒絕並附上指示
            p.resolve({"behavior": "deny", "message": f"The user denied this action and said: {text}"})
            tg.send(chat_id, "↩️ 已拒絕，並把你的話轉告 Claude", silent=True)
    elif text.startswith("/"):
        handle_command(chat_id, text)
    elif chat_state(chat_id)["target_pid"]:
        send_to_cli(chat_id, text)
    else:
        tg.send(chat_id, NO_TARGET)


BOT_COMMANDS = [
    ("cli", "選擇要接管的 CLI"), ("esc", "中斷接管中的 CLI"), ("bg", "放開 CLI"),
    ("clear", "清除對話（轉送給接管中的 CLI）"), ("status", "目前狀態"),
    ("id", "顯示 chat id"), ("help", "說明"),
]

def reset_bot_ui():
    """清掉所有範圍殘留的指令、選單按鈕與自訂鍵盤，再登記本專案的指令。"""
    scopes = [{"type": t} for t in ("default", "all_private_chats", "all_group_chats",
                                    "all_chat_administrators")]
    for cid in CFG["allowed_chat_ids"]:
        scopes.append({"type": "chat", "chat_id": cid})
        if cid < 0:  # 群組才有管理員範圍
            scopes.append({"type": "chat_administrators", "chat_id": cid})
    for sc in scopes:
        tg.call("deleteMyCommands", scope=sc)
    tg.call("setMyCommands", commands=[{"command": c, "description": d} for c, d in BOT_COMMANDS])
    for cid in CFG["allowed_chat_ids"]:
        tg.call("setChatMenuButton", chat_id=cid, menu_button={"type": "commands"})
        tg.call("sendMessage", chat_id=cid, disable_notification=True,
                text="🟢 中繼已上線，輸入 / 選擇指令",
                reply_markup={"remove_keyboard": True})  # 移除輸入框下方殘留的按鈕


def main():
    if not CFG.get("bot_token"):
        sys.exit("請先在 local/config.json 設定 bot_token（可複製 local/config.example.json）")
    me = tg.call("getMe")
    if not me.get("ok"):
        sys.exit(f"bot_token 無效: {me}")
    log.info("Bot @%s 已啟動", me["result"]["username"])
    start_local_server()
    threading.Thread(target=_flusher, daemon=True).start()
    reset_bot_ui()

    offset = None
    while True:
        res = tg.call("getUpdates", _timeout=60, _retry=0, offset=offset, timeout=50,
                      allowed_updates=["message", "callback_query"])
        if not res.get("ok"):
            time.sleep(3)
            continue
        for u in res["result"]:
            offset = u["update_id"] + 1
            try:
                handle_update(u)
            except Exception:
                log.exception("處理更新失敗")


if __name__ == "__main__":
    main()
