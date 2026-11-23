# PLAN：agent-proxy v2 — 插件 + asyncRewake

> 狀態：規劃中（分支 `feat/channel-mcp`）。先做 Windows，驗證後移植 Mac（預期不改程式碼）。

## 1. 目標

用 Telegram 遠端**監控、確認、輸入**電腦上**已經開著**的 Claude Code session。

- 不接管、不打字進終端機、不需特殊方式啟動 CLI
- 適用 CLI（任何終端機）、VSCode、Claude 桌面 App（hooks 在各處都會觸發）
- 只轉播主 session，子代理內部事件不轉播

## 2. 已驗證

| 項目 | 結果 |
|---|---|
| `asyncRewake` hook 在 session **閒置時**叫醒 Claude 並執行手機指令（含工具呼叫） | ✅ 2026-11-23 PoC，連續兩輪成功 |
| HTTP hooks（`type: http`）送達本機服務 | ✅ Channel 版實測 |
| 桌面 App 觸發 hooks | ⏳ 文件有寫，未實測 |
| 插件 `userConfig` 值可用於 HTTP hook header 的環境變數插值 | ⏳ 未驗證（不行則改 async command hook） |

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

### 4.1 Telegram 訊息：每回合一則「回合卡片」，以 edit 更新

一回合（一次輸入 → Claude 完成）只佔**一則訊息**，分兩段，進度變化時 `editMessageText` 原地更新：

```
👤 [agent-proxy] 電腦端                      ← 第一段：我輸入的內容
幫 relay 加上 edit 模式的通知
━━━━━━━━━━━━
🔄 執行中 · 1m23s · 工具 12 次               ← 第二段：當前進度總結
📋 ✅ 讀取現有程式
   🔄 改寫推送邏輯
   ⬜ 測試
最近：✏️ Edit daemon.py
```

完成時第二段換成結果：

```
👤 [agent-proxy] 手機
幫 relay 加上 edit 模式的通知
━━━━━━━━━━━━
✅ 完成 · 2m10s · 工具 18 次
<Claude 最後的回覆>
```

規則：
- **開卡**：`UserPromptSubmit`（電腦輸入）或手機訊息送出時建立；來源標「電腦端／手機」
- **進度段**內容優先序：Todo 清單 → 最近一個工具動作 → 耗時與工具次數
- **節流**：同一張卡片至少間隔 `edit_interval`（預設 3 秒）才 edit；內容相同不 edit（避免 `message is not modified`）
- **完成通知**：edit 不會讓手機響。完成時**刪除進度卡、重發一則完成卡**（有通知）；`notify_on_done: false` 則只 edit
- **過長**：回覆超過 4096 字時，卡片放前段，其餘接續新訊息
- **權限確認、Claude 提問**另發獨立訊息（需要通知與按鈕），處理後 edit 標示結果
- 卡片狀態（message_id、開始時間、工具數、todo）存在 daemon 記憶體，以 session 為單位

### 4.2 手機輸入

- 一般文字 → 交給目標 session 的 `wait_phone`，叫醒 Claude。文字固定包成
  `The user sent this from their phone (Telegram): …`
- Claude 執行中收到 → 排隊，該回合結束後送出，卡片顯示「⏳ 已排隊」
- 斜線指令、Esc 無法送出，回覆說明

### 4.3 權限確認

- `/away`：PermissionRequest 等手機按鈕（允許／拒絕／直接回文字＝拒絕並改給指示），`away_timeout` 秒未回應則退回電腦視窗
- `/back`（預設）：立即放行給電腦視窗，手機只收通知
- 電腦與手機**無法同時**確認（hooks 限制），以切換取代接管

### 4.4 多 session

- 目標 session 預設為「最近有活動」的那個；卡片都標專案名
- `/to`：列出開著的 session（按鈕）切換目標
- 只開一個 session 時完全無感

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
| M1 | daemon + hooks 回報 + 回合卡片 edit 模式 | 電腦輸入一句，手機出現一張卡片並隨進度更新，完成時響通知 |
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
