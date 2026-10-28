# ArQ 資安設計

ArQ 能執行 PowerShell、截圖、改程式，所以預設不信任任何輸入。以下是刻意設計且有測試鎖定的不變量。

## 身分與權限

- **白名單預設拒絕**：`ALLOWED_USER_IDS` 為空時所有人被拒；每個 Slash 指令都先檢查身分。
- **風險操作先問**：安裝套件、刪檔、改系統設定、`git commit / push / merge` 由 `risk_policy` 判定，在 Discord 跳按鈕請你同意，連 RD 開發途中也一樣。
- **fail-closed**：沒有同意回呼、逾時（15 分鐘）、拋例外、事件迴圈已關閉，一律視為拒絕。
- **不啟用 Gemini SDK 的自動函式呼叫**：工具迴圈是手動的，每個 `function_call` 都先過 guard 與 risk。
- **危險參數不給 AI**：`cwd`、`timeout`、`approve` 等不對 Gemini 開放；`scope` 只接受 `task` / `core`。

## 開發流程的人工關卡

- `git commit` 在風險規則中，RD 與 AI 自行 commit 會被攔下；`git push` 不會自己發生。
- **同意 commit 不等於同意 merge**：merge 必須單獨一行執行並跳獨立確認卡（PR 標題、對應 ticket、review 數），夾帶其他指令直接拒絕。
- PM 不讀程式碼：讀 `task/` 以外的程式碼檔或執行 `git diff` 由 guard 直接拒絕，需要了解現有程式時改派 RD 只讀規劃。
- `scope=core` 在該單專屬的 git worktree 內開發，執行中的 ArQ 資料夾保持在 `main`。

## 金鑰

- 金鑰只放 `.env`，不得出現在程式碼、文件、測試與 `.env.example`；`.env` 讀寫本身是風險規則。
- 子程序一律使用過濾後的環境（`secret_guard.child_env()`），AI 與 RD 讀不到金鑰；送出前、日誌與對話記憶存檔前都會遮蔽。
- API 金鑰走 Header 不進 URL；`call_api` 的金鑰需綁定主機，交易類操作不開放。
- 永豐證券整合**唯讀**：程式不含任何下單 / 改單 / 刪單呼叫（測試會掃描原始碼）。

## 外部內容與 MCP

- 搜尋結果、檔案、MCP 工具回傳都只是資料，其中的指示不照做。
- MCP server 視為不可信第三方程式：新增或變更的 server 必須在 `/mcp reload` 核可才啟動；每次呼叫預設跳同意卡；金鑰只能以 `${ENV_VAR}` 引用且不得引用 ArQ 自身金鑰；工具描述清除控制字元並截斷。

## 公開 Repo 檢查

repo 一律視為公開：`tests/test_public_safety.py` 會掃描所有進版控的檔案，不得含 `.env` 的實際值、個人家目錄路徑或常見金鑰樣式。執行期資料（對話記憶、日誌、派工狀態）只存在不進版控的 `_temp/`。
