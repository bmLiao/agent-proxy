# agent-proxy：用 Telegram 遙控 Claude Code

```
手機 Telegram ⇄ relay.py ⇄ 電腦上開著的互動式 CLI（cli_hook.py 登記 + 主控台打字）
```

relay 只接管已經開著的 CLI，**不會自己啟動 claude**；沒接管時手機訊息不會被執行。

只用 Python 標準庫，不需要 `pip install`。目前僅支援 Windows（打字進 CLI 用的是 Windows 主控台 API）。

## 安裝

1. `@BotFather` → `/newbot` 取得 token。
2. `copy local\config.example.json local\config.json`，填 `bot_token`、`allowed_chat_ids`、`allowed_user_ids`。
   不知道 id 就先啟動 relay，對 bot 傳 `/start`，它會告訴你。
   - `local/`：私密與本機資料（`config.json` 機器人資訊、`state.json` 接管狀態），除了範本外都不進版控。
   - 根目錄 `config.json`：一般設定（`flush_interval`、`show_tool_calls`、`cli_approval_timeout`），可提交；
     兩者同名欄位以 `local/config.json` 為準。
3. `python install_hook.py`：把 hook 加到 `%CLAUDE_CONFIG_DIR%\settings.json`（會備份 `.bak`）。
   **已開著的 CLI 要重開**才會載入 hook。移除用 `python install_hook.py --uninstall`。
4. `python relay.py`（長期使用可在工作排程器設定登入時執行 `pythonw relay.py`）。

## 接管電腦上的 CLI

1. 手機 `/cli` → 列出所有已登記的 CLI（💤 閒置、🔄 執行中、🔐 等待確認），點一個接管。
2. 接管後：
   - 電腦端輸入（👤）、工具呼叫、📋 Todo 進度、回合完成的回覆，都會即時傳到手機
   - 手機**直接打字 = 打進那個 CLI**，執行中送出的文字會排進 CLI 的佇列
   - `/clear`、`/compact`、`/model` 等不是 relay 的指令，會**原樣轉送給 CLI**
   - `/esc`：送 Esc 中斷 CLI
   - 權限請求推到手機（允許、拒絕、之後都允許，或直接打字＝拒絕並給新指示）；`cli_approval_timeout` 秒沒回應就退回電腦端確認
3. `/bg` 放開 CLI，或 `/cli` 換一個接管。

沒被接管的 CLI 完全不受影響，hook 只做登記，馬上放行。

限制：
- 打字功能需要 CLI 跑在終端機裡（Windows Terminal、VSCode 內建終端機、cmd 都可以）。VSCode 的 Claude **擴充面板**沒有主控台，只能監控與授權。
- 如果 CLI 正在電腦上跳確認視窗，relay 會拒絕送出文字，避免打進視窗裡。

## 檔案

| 檔案 | 用途 |
|---|---|
| `relay.py` | 主程式：Telegram 長輪詢、CLI 登記/接管、本機 HTTP 端點 |
| `cli_hook.py` | Claude Code hook：回報事件、找出 CLI 的 pid、轉送權限請求 |
| `console_inject.py` | 把文字或按鍵寫進指定 CLI 的主控台 |
| `install_hook.py` | 安裝/移除 hook（`--print` 只輸出 JSON，可給 `claude --settings`） |
| `tgapi.py` | Telegram API、設定讀取 |

安全性：本機端點只聽 `127.0.0.1`，token 每次啟動隨機產生（寫在 `.runtime/relay.json`）；
Telegram 端只接受白名單內的 chat 與使用者。能控制 bot 就等於能在你電腦上執行指令，請保管好 token。
