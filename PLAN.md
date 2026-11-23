# PLAN：agent-proxy v2 — 插件 + asyncRewake

> 狀態：M1–M5 完成（分支 `feat/channel-mcp`）；M6（桌面 App、Mac）待實測。

## 1. 目標

用 Telegram 遠端**監控、確認、輸入**電腦上**已經開著**的 Claude Code session。

- 不接管、不打字進終端機、不需特殊方式啟動 CLI
- 適用 CLI（任何終端機）、VSCode、Claude 桌面 App（hooks 在各處都會觸發）
- 只轉播主 session，子代理內部事件不轉播

## 2. 已驗證

| 項目 | 結果 |
|---|---|
| `asyncRewake` hook 在 session **閒置時**叫醒 Claude 並執行手機指令（含工具呼叫） | ✅ 2026-11-23 PoC，連續兩輪成功 |
| HTTP hooks（`type: http`）送達本機服務 | ✅ 實測 |
| 桌面 App 觸發 hooks | ⏳ 文件有寫，未實測 |
| 手機輸入、排隊、away 權限按鈕（真實互動 session） | ✅ 2026-11-23 實測 |
| 插件 `userConfig` 以環境變數傳給 daemon | ⏳ 未實測（另可用 config.json） |
| `claude -p` 等非互動 session 不受影響（以 `CLAUDE_CODE_SESSION_ATTENDED=0` 判斷並略過） | ✅ 實測 |
| 桌面 App 的 session 是否為 `ATTENDED=1` | ⏳ 未實測（若為 0 插件會略過它） |

## 3. 架構

```
手機 Telegram ⇄ daemon.py（常駐：Telegram 長輪詢 + 本機 HTTP 127.0.0.1:<固定 port>）
                     ↑↓
   插件 hooks（全域，每個 session 都有）
     ├─ SessionStart  (command) ensure_daemon：daemon 沒在跑就拉起來
     ├─ 事件回報      (http)    UserPromptSubmit / PostToolUse / Notification / Stop
     ├─ PermissionRequest (http) /away 時等手機決定；/back 時立即放行給電腦
     └─ SessionStart+Stop (command, asyncRewake) wait_phone：向 daemon 長輪詢，
                               收到手機訊息 → stderr 輸出、exit 2 → 叫醒 Claude
```

## 4. 功能規格

### 4.1 Telegram 訊息：整個聊天室只維持「同一張卡」

整個聊天室只有**一張卡片**（顯示鎖定的 session），跨回合、跨 session 切換都沿用；聊天室裡不留其他訊息（我的輸入會被刪除）。
卡片固定兩段，新回合開始時第一段換成新的輸入、第二段重置：

```
🟢 agent-proxy · 🔄 執行中                   ← 標頭：專案、狀態（away 模式時加「· away」）
━━━━━━━━━━━━
👤 幫我跑測試並修好失敗的                    ← 第一段：我輸入的內容（最新一次）
━━━━━━━━━━━━
🔄 1m23s · 工具 12 次                        ← 第二段：當前進度總結
✅ 讀取現有程式
🔄 改寫推送邏輯
⬜ 測試
```

完成時第二段換成結果：`✅ 完成 · 2m10s · 工具 18 次` + Claude 最後的回覆。

更新方式分兩種：

| 情況 | 做法 | 手機會響 |
|---|---|---|
| 進度變化（工具呼叫、Todo、排隊中） | `editMessageText` 原地更新 | 否 |
| 需要我注意（回合完成、權限確認、Claude 提問） | **刪除舊卡 → 重發同內容的新卡**（仍只有一張，移到最下方） | 是 |

規則：
- **手機輸入不留原訊息**：daemon 接收手機文字後先 `deleteMessage` 刪掉我那則，再 edit 卡片第一段；
  卡片即「已收到」回執，不另發確認。刪除失敗（如群組無權限）則保留原訊息，不影響流程
- **電腦輸入**（`UserPromptSubmit`）同樣更新第一段（不標來源）
- **進度段**：耗時、工具次數 + Todo／Task 清單；不顯示個別工具動作
- **權限確認／提問直接放在卡片上**：卡片第二段顯示待確認內容並掛上按鈕（允許／拒絕／選項），處理後按鈕移除、恢復進度顯示
- **節流**：至少間隔 `edit_interval`（預設 3 秒）才 edit；內容相同不 edit（避免 `message is not modified`）
- **過長**：回覆依規則本應精簡；超過卡片上限（4096 字）時截斷加「…」並附「📄 看全文」按鈕，按下另發完整一篇（自動分段）
- **卡片延續**：卡片 message_id 存檔（daemon 重啟後繼續 edit 同一張）；
  若 edit／delete 失敗（卡片被手動刪除或超過 Telegram 48 小時刪除限制），直接發一張新卡取代
- `notify_on_done: false`：完成時也只 edit 不重發（不響、卡片不移動）

### 4.2 手機輸入

- 一般文字 → 交給目標 session 的 `wait_phone`，叫醒 Claude。文字固定包成
  `The user sent this from their phone (Telegram): …`
- 流程：接收 → 刪除我的原訊息 → edit 卡片第一段、第二段顯示「🕐 已送出，等待 Claude」→ Claude 開始後轉為進度更新
- Claude 執行中收到 → 同樣刪除原訊息，卡片第二段附註「⏳ 已排隊：…」，該回合結束後送出
- 權限確認時直接回文字：刪除原訊息，視為拒絕並改給指示，卡片顯示「↩️ 已拒絕並改給指示」
- 斜線指令、Esc 無法送出，回覆說明

### 4.3 權限確認

- `/away`：PermissionRequest 等手機按鈕（允許／拒絕／直接回文字＝拒絕並改給指示），`away_timeout` 秒未回應則退回電腦視窗
- `/back`（預設）：立即放行給電腦視窗，手機只收通知
- 電腦與手機**無法同時**確認（hooks 限制），以切換取代接管

### 4.4 多 session：一次只鎖定一個

- Telegram 一次只鎖定一個 session；第一個開的自動鎖定，之後開的**不搶**，只有在 Telegram 上 `/to` 才替換
- 只有鎖定的 session 上卡片、收手機訊息、權限推到手機；其他 session 的權限照常在電腦端確認
- 替換時：舊 session 的待確認退回電腦、尚未送出的手機訊息作廢，卡片（同一張）改顯示新 session
- 鎖定的 session 結束：卡片標示已結束並釋放鎖定，下一個開始活動的 session 自動接手
- 同一 session 內平行子代理的權限確認排隊，卡片一次顯示一筆

### 4.5 指令

`/status`（目標、狀態、away/back）、`/to`、`/away`、`/back`、`/id`

## 5. 設計重點（優化）

1. **事件回報用 HTTP hooks**：不為每個事件開 Python 程序（Windows 每次省 50–100ms）；只有 SessionStart、wait_phone 用 command hook
2. **每 session 只留一個 wait_phone**：新的上線時 daemon 讓舊的回應 `superseded` 並結束（PoC 時會累積）
3. **hook 不阻塞 CLI**：daemon 立即回 `{}`，推送在背景佇列依序處理（PermissionRequest 的 away 模式除外）
4. **子代理事件略過**：帶 `agent_id` 的事件不處理
5. **daemon 自動生命週期**：SessionStart 拉起、以綁定固定 port 當單一實例鎖、所有 session 結束 N 分鐘後自行退出
6. **設定走插件 `userConfig`**：bot token（sensitive）、允許的 chat/user id，不再需要 `local/config.json`
7. **單一程式碼跨平台**：不碰終端機與 OS API，只用 Python 標準庫

## 6. 檔案結構

```
agent-proxy/                     ← marketplace 根目錄
  .claude-plugin/marketplace.json
  plugin/
    .claude-plugin/plugin.json   ← name、userConfig
    hooks/hooks.json
    scripts/ensure_daemon.py     ← SessionStart
    scripts/wait_phone.py        ← asyncRewake
    daemon/daemon.py             ← Telegram + HTTP + 回合卡片
    daemon/tgapi.py
  PLAN.md  README.md
```

移除：`relay.py`、`cli_hook.py`、`console_inject.py`、`install_hook.py`、`channel.py`、`cctg.py`、`config.json`
（`channel.py` 的推送／批次邏輯移入 daemon）。

安裝：`claude plugin marketplace add <repo 路徑>` → `claude plugin install agent-proxy@agent-proxy`。

## 7. 里程碑與驗收

| # | 內容 | 驗收 |
|---|---|---|
| M1 | daemon + hooks 回報 + 單一卡片 edit 模式 | 連續多回合都只有同一張卡；進度原地更新，完成時卡片重發並響通知 |
| M2 | wait_phone 手機輸入 + 排隊 | 已開閒置 session 從手機下指令可執行；執行中送出會排隊 |
| M3 | 權限 `/away` `/back` | away 時手機按鈕生效；back 時電腦立即跳視窗 |
| M4 | 多 session `/to`、daemon 自動結束 | 兩個 session 可切換；全關 N 分鐘後 daemon 結束 |
| M5 | 插件打包、README、移除舊檔 | 乾淨環境照 README 安裝可用 |
| M6 | 桌面 App、Mac 實測 | 兩者跑過 M1–M3 |

## 8. 風險

| 風險 | 對策 |
|---|---|
| asyncRewake 訊息是系統提醒，未來版本可能較保守 | 固定格式標明來源；備案為官方 `/remote-control` |
| userConfig 無法插值進 HTTP header | 事件回報改 `async: true` 的 command hook |
| Telegram edit 限流（429） | 節流 + 遵守 `retry_after`（tgapi 已處理） |
| 安全：能操作 bot = 能在電腦執行指令 | user id 白名單、只收 private chat、token 走 sensitive userConfig |

## 9. 不做

斜線指令與 Esc 轉送、裝插件前就開著的 session、電腦與手機同時確認權限。
