# PLAN：agent-proxy v2 — 插件 + asyncRewake

> 狀態：M1–M5 完成（已合併到 `master`）；M6（桌面 App、Mac）待實測。插件尚未安裝到全域設定。

## 1. 目標

用 Telegram 遠端**監控、確認、輸入**電腦上**已經開著**的 Claude Code session。

- 不接管、不打字進終端機、不需特殊方式啟動 CLI
- 適用 CLI（任何終端機）、VSCode、Claude 桌面 App（hooks 在各處都會觸發）
- Telegram 一次只鎖定一個 session；只轉播主 session，子代理內部事件不轉播

## 2. 已驗證

| 項目 | 結果 |
|---|---|
| `asyncRewake` hook 在 session **閒置時**叫醒 Claude 並執行手機指令（含工具呼叫） | ✅ 實測 |
| HTTP hooks（`type: http`）送達本機 daemon | ✅ 實測 |
| 手機輸入、排隊、away 權限按鈕（真實互動 session） | ✅ 實測 |
| 背景任務：只在全部做完時響一次，卡片輸入維持我打的字 | ✅ 實測 |
| 同時開兩個 CLI：第二個不搶鎖定，手機訊息送往鎖定的 session | ✅ 實測 |
| 卡片標頭顯示視窗標題（ai-title） | ✅ 實測 |
| 平行子代理共用同一個 `session_id`（以 `agent_id` 區分） | ✅ 實測 |
| `claude -p` 等非互動 session 不受影響、不出現 hook 錯誤 | ✅ 實測 |
| CLI 視窗直接關閉：waiter 結束、session 自動清除 | ✅ 實測 |
| hooks 以 `python3` 呼叫（Windows Git Bash） | ✅ 實測 |
| `/to` 切換、`/clear` 交接、子代理權限排隊、Esc 後「已停止」 | 🟡 模擬測試 |
| 插件 `userConfig` 以環境變數傳給 daemon | ⏳ 未實測（另可用 config.json） |
| 桌面 App 觸發 hooks、其 session 是否為 `ATTENDED=1` | ⏳ 未實測（若為 0 插件會略過它） |
| Mac | ⏳ 未實測 |

## 3. 架構

```
手機 Telegram ⇄ daemon.py（常駐：Telegram 長輪詢 + 本機 HTTP 127.0.0.1:47821）
                     ↑↓
   插件 hooks（全域，每個 session 都有）
     ├─ SessionStart (command) ensure_daemon：daemon 沒在跑（或版本不符）就拉起來；互動 session 才註冊
     ├─ 事件回報 (http)        UserPromptSubmit / PostToolUse / Notification / Stop / SessionEnd
     ├─ PermissionRequest (http) away 時等手機決定；back 時立即交給電腦
     └─ SessionStart+Stop (command, asyncRewake) wait_phone：向 daemon 長輪詢，
                               收到手機訊息 → stderr 輸出、exit 2 → 叫醒 Claude
```

## 4. 功能規格

### 4.1 Telegram 卡片：整個聊天室只有一張

```
🔒 咖啡店命名建議 · 🔄 執行中 · 🚶 away（自動）  ← 標頭：視窗標題、狀態、away 時標模式
（另有 1 個 session，/to 可切換）                ← 有其他 session 時才出現
━━━━━━━━━━━━
👤 幫我跑測試並修好失敗的                        ← 第一段：我實際打的內容（最新一次）
━━━━━━━━━━━━
🔄 1m23s · 工具 12 次                            ← 第二段：當前進度
✅ 讀取現有程式
🔄 改寫推送邏輯
⬜ 測試
```

完成時第二段換成 `⏱ 2m10s · 工具 18 次` + Claude 最後的回覆。

- **標題**：`/rename` 自訂的 → Claude Code 產生的 ai-title（讀 transcript）→ 專案資料夾名；每 15 秒重讀
- **狀態**：💤 閒置、🕐 已送出、🔄 執行中、⏳ 背景執行中、✅ 完成、⏹ 已停止（沒有 Stop 就閒置，多半是 Esc）、⚫ 已結束、🔐 待確認
- **第一段只放我實際打的字**：Claude Code 自動送進來的（`<task-notification>`、子代理回報、手機訊息的叫醒回饋等）視為接續，不換輸入、不重置計時
- **更新方式**：進度變化 → 原地 edit（不響，間隔至少 `edit_interval` 秒）；需要我注意（任務完成、權限確認、提問）→ 刪卡重發（響）
- **完成判定**：Stop 時還有背景任務在跑 → ⏳ 背景執行中、不響；全部做完的 Stop 才 ✅ 完成並響一次
- **過長**：截斷加「…」並附「📄 看全文」按鈕，按下另發完整一篇
- **卡片範圍**：daemon 只管理本次啟動後開的那張卡，之前留下的舊卡不追究
- `notify_on_done: false`：完成時也只 edit 不重發

### 4.2 手機輸入

- 文字 → 交給鎖定 session 的 `wait_phone` 叫醒 Claude，包成 `The user sent this message from their phone (Telegram); treat it as their prompt:`
- 流程：收到 → 刪除我的原訊息 → 卡片第一段換成這句、第二段「🕐 已送出」→ Claude 開始後轉為進度
- Claude 執行中收到 → 卡片附「⏳ 已排隊」，該回合結束後送出；背景任務執行中則可直接送出
- 有待確認時直接回文字 → 拒絕並改給指示；有提問時 → 當作答案
- 斜線指令、Esc 無法送出；指令回覆是暫時訊息，數十秒後自動刪除

### 4.3 權限確認

- **away**：PermissionRequest 掛在卡片上等手機按鈕（允許／拒絕／之後都允許），`away_timeout` 秒未回應退回電腦
- **back**：立即交給電腦視窗，卡片只註記
- **自動切換**：手機傳訊息 → away；電腦端送出輸入 → back（手機上未按的確認退回電腦）；`/away`、`/back` 可手動覆寫
- 平行子代理同時要確認 → 排隊，卡片一次顯示一筆（標示「🤖 子代理（還有 N 筆）」），輪到下一筆時再響
- 手機上的確認在這些情況自動收掉：工具已執行（電腦端處理掉）、主代理已停止、切換 session、session 結束
- 電腦與手機無法同時確認（hooks 限制）

### 4.4 Session 鎖定

- 第一個開的互動 session 自動鎖定，之後開的**不搶**；只有在 Telegram 上 `/to` 才替換
- 只有鎖定的 session 上卡片、收手機訊息、權限推到手機；其他 session 照常在電腦端
- `/to` 替換：舊 session 的待確認退回電腦、尚未送出的手機訊息作廢，卡片改顯示新 session
- 鎖定的 session 結束：15 秒內新開的 session（例如 `/clear` 換的新 session id）直接接手，
  並帶上還沒送出的手機訊息；否則卡片標示已結束，下一個開始活動的 session 接手
- CLI 視窗被直接關閉（沒有 SessionEnd）：waiter 偵測 Claude 行程已結束而退出，約 1–2 分鐘後清除並釋放鎖定

### 4.5 指令

`/status`、`/to`、`/away`、`/back`、`/id`

## 5. 設計重點

1. **事件回報用 HTTP hooks**：不為每個事件開 Python 程序；只有 SessionStart、wait_phone 用 command hook（`python3`）
2. **hook 不阻塞 CLI**：daemon 立即回應，事件在背景佇列依序處理；卡片的 Telegram 呼叫在專屬執行緒，
   Telegram 慢或斷線時不影響 hook 與電腦端確認
3. **每 session 只留一個 waiter**：新的上線時舊的結束；Claude 行程不在了 waiter 也結束
4. **非互動 session 略過**：`CLAUDE_CODE_SESSION_ATTENDED=0`（`claude -p`）只確保 daemon 在跑、不註冊
5. **daemon 生命週期**：SessionStart 拉起；固定 port 當單一實例鎖；版本＝程式內容雜湊（不同設定目錄的同版安裝不互相替換）；
   沒有 session 也沒有任何 hook 事件 30 分鐘後自行結束；重啟前 2 分鐘內的手機訊息照樣處理
6. **設定**：插件 `userConfig`（bot token 為 sensitive）或插件資料夾的 `config.json`
7. **跨平台**：只用 Python 標準庫，不碰終端機；唯一的 OS 專用碼是 waiter 檢查行程是否存在

## 6. 檔案結構

```
agent-proxy/                     ← marketplace 根目錄
  .claude-plugin/marketplace.json
  plugin/
    .claude-plugin/plugin.json   ← name、userConfig
    hooks/hooks.json
    scripts/ensure_daemon.py     ← SessionStart
    scripts/wait_phone.py        ← asyncRewake
    daemon/daemon.py             ← Telegram + HTTP + 卡片 + 鎖定 + 權限
    daemon/tgapi.py              ← Telegram API、設定、版本、行程檢查
  PLAN.md  README.md
```

## 7. 里程碑

| # | 內容 | 狀態 |
|---|---|---|
| M1 | daemon + hooks 回報 + 單一卡片 edit 模式 | ✅ |
| M2 | wait_phone 手機輸入 + 排隊 | ✅ |
| M3 | 權限 away / back（含自動切換） | ✅ |
| M4 | 多 session 鎖定、`/to`、daemon 自動結束 | ✅ |
| M5 | 插件打包、README、移除舊檔 | ✅ |
| M6 | 桌面 App、Mac 實測 | ⏳ |

## 8. 風險

| 風險 | 對策 |
|---|---|
| asyncRewake 訊息是系統提醒，未來版本可能較保守 | 固定格式標明來源；備案為官方 `/remote-control` |
| 桌面 App 的 session 被判定為非互動而略過 | M6 實測確認 |
| Telegram 限流（429） | 節流 + 遵守 `retry_after` |
| 本機 HTTP 介面沒有驗證 | 只聽 127.0.0.1；風險低，維持現狀 |
| 安全：能操作 bot = 能在電腦執行指令 | user id 白名單、token 走 sensitive userConfig |

## 9. 不做

斜線指令與 Esc 轉送、裝插件前就開著的 session、電腦與手機同時確認權限、`/loop` 等排程訊息與手打輸入的區分。
