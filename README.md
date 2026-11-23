# agent-proxy：用 Telegram 遠端操作 Claude Code

Claude Code 插件。在手機上**監控、確認、輸入**電腦上已經開著的 Claude Code session：
不需接管、不打字進終端機、不用特殊方式啟動，終端機、VSCode、桌面 App 都適用，Windows / Mac 同一套。

```
手機 Telegram ⇄ daemon（自動啟動）⇄ 插件 hooks ⇄ 你開著的 claude session
```

## 功能

- **一次只鎖定一個 session**：第一個開的 session 自動鎖定，之後開的不會搶；只有在 Telegram 上用 `/to` 才替換。
  鎖定的 session 結束後，下一個開始活動的 session 自動接手
- **整個聊天室只有一張卡片**：標頭顯示鎖定 session 的視窗標題（/rename 自訂的或 Claude Code 自動產生的），內容是我的輸入 + 當前進度（耗時、工具次數、Todo），原地更新；
  完成時卡片重發（手機會響），回覆太長可按「📄 看全文」
- **手機輸入**：直接打字＝對 Claude 說話（閒置時立刻叫醒，執行中自動排隊）；你傳的訊息收到後會刪除，聊天室只留卡片
- **權限確認**：away 時推到手機（允許／拒絕／之後都允許，或直接回文字＝拒絕並改給指示，也能回答 Claude 的提問）；
  back 時由電腦端確認。**自動切換**：從手機傳訊息 → away；在電腦端送出輸入 → back（手機上還沒按的確認改回電腦）。
  也可用 `/away`、`/back` 手動切換，下次輸入時會再依來源自動判斷
- **只有鎖定的 session** 會上卡片、收手機訊息、把權限確認推到手機；其他 session 照常在電腦端操作
- **指令**：`/status` 狀態、`/to` 切換鎖定的 session、`/away`、`/back`
- 子代理內部事件不轉播

## 安裝

需要 Python 3.9+（只用標準庫）與 Claude Code。

1. `@BotFather` → `/newbot` 取得 bot token。
2. 安裝插件：
   ```
   claude plugin marketplace add <這個 repo 的路徑或 GitHub owner/repo>
   claude plugin install agent-proxy@agent-proxy
   ```
3. 在 Claude Code 執行 `/plugin configure agent-proxy@agent-proxy`，填 bot token、允許的 chat id / user id。
   不知道 id：先只填 token，開一個新 session，對 bot 傳 `/id`。
4. **之後開的 session** 都會自動連上（裝插件前就開著的要重開）。

也可以不用插件設定，改放 `<Claude 設定目錄>/plugins/data/agent-proxy-agent-proxy/config.json`：

```json
{ "bot_token": "123:ABC", "allowed_chat_ids": [123456789], "allowed_user_ids": [123456789] }
```

其他可調：`edit_interval`（卡片更新最小間隔，預設 3 秒）、`notify_on_done`（完成時重發卡片讓手機響，預設 true）、
`away_timeout`（away 時手機多久沒回應退回電腦，預設 600 秒）、`port`（預設 47821）。

開發時可直接載入：`claude --plugin-dir ./plugin`（設定讀 repo 的 `local/config.json`）。

## 限制

- 手機無法送 `/clear`、`/compact` 等斜線指令，也無法 Esc 中斷
- 權限確認是二選一：away 時由手機決定，back 時由電腦決定
- 手機訊息以系統提醒的形式交給 Claude（asyncRewake），不是一般的使用者輸入
- 同一個 bot 同時只能有一個程式在收訊息

## 檔案

| 檔案 | 用途 |
|---|---|
| `plugin/hooks/hooks.json` | 掛上的 hooks：事件用 HTTP 送 daemon，SessionStart 拉起 daemon，asyncRewake 等手機訊息 |
| `plugin/daemon/daemon.py` | Telegram 長輪詢、卡片、權限、手機訊息派送 |
| `plugin/daemon/tgapi.py` | Telegram API 與設定讀取 |
| `plugin/scripts/ensure_daemon.py` | SessionStart：daemon 沒在跑（或版本不符）就啟動 |
| `plugin/scripts/wait_phone.py` | asyncRewake：等手機訊息，收到就叫醒 Claude |

log 與狀態在插件資料夾（`daemon.log`、`state.json`）。

安全性：daemon 只聽 `127.0.0.1`；Telegram 只接受白名單內的 chat 與使用者。
能操作這個 bot 就等於能在你的電腦上執行指令，請保管好 token。
