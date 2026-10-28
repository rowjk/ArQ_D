"""
風險判定政策 (決定 AI 的操作要不要先問使用者)

使用者規則 (修改時必須維持)：
    1. 只要需要安裝新套件，一定要先獲得使用者同意。
    2. 其他任務若沒有風險，可直接執行，不必確認。
    3. 有風險 (破壞性、不可逆、對外推送) 的操作，需先獲得使用者同意。

本模組提供：
    classify_risk(text)       → 回傳需要確認的理由 (str)；None 代表可直接執行。
                                用於 Gemini 的 PowerShell 指令 / Python 程式碼，以及 Claude Code 的每個 shell 指令。
    report_new_packages(...)  → 事後比對 ArQ 環境套件快照，抓出「繞過指令比對」的安裝並通知使用者。

判定方式是「關鍵字樣式比對」，屬於盡力而為的防護：
    - 寧可多問 (誤判為有風險) 也不要漏問。
    - 新增套件管理工具或危險指令時，請補進下方樣式清單，並在 GEMINI.md 的測試清單驗證。
"""

import re
from contextlib import contextmanager
from importlib import metadata
from typing import Callable, Dict, Iterator, List, Optional

# -------------------------------------------------------------
# 1. 安裝套件 (一律需要同意)
# -------------------------------------------------------------
_INSTALL_PATTERNS = [
    r"\bpip3?(\.exe)?\s+install\b",                      # pip install x
    r"-m\s+pip\s+install\b",                             # python -m pip install x
    r"\bpip3?['\"]?\s*,\s*['\"]install\b",               # subprocess.run(["pip", "install", ...])
    r"\buv\s+(pip\s+install|add|tool\s+install)\b",      # uv pip install / uv add
    r"\b(npm|pnpm|yarn|bun)\s+(install|i|add)\b",        # Node 套件
    r"\b(winget|choco|scoop|conda|mamba)\s+install\b",   # 系統 / conda 套件
    r"\bInstall-(Module|Package)\b",                     # PowerShell 模組
    r"\bsetup\.py\s+(install|develop)\b",                # python setup.py install（寫進腳本間接安裝的常見手法）
    r"\beasy_install\b",                                 # 舊式 setuptools 安裝工具
]
_INSTALL_RE = re.compile("|".join(_INSTALL_PATTERNS), re.IGNORECASE)

# -------------------------------------------------------------
# 2. 高風險操作 (需要同意)：(樣式, 給使用者看的理由, 適用範圍)
#    範圍 "shell" = PowerShell / Bash 指令；"python" = Python 程式碼。
#    rm / del / rd 等短字只套用在 shell，避免 Python 的 `del x[0]` 被誤判。
#    Python 程式碼常以 subprocess 呼叫 shell，因此「辨識度高」的 shell 規則也套用到 python。
# -------------------------------------------------------------
_BOTH = ("shell", "python")
# git 與子指令之間的全域選項 (-C <路徑>、-c k=v、--git-dir=…、--no-pager 等)：
# worktree 流程 用 git -C <worktree> commit / push，不能讓守門因此失效
_GIT = (r"\bgit(?:\s+(?:-C\s+(?:\"[^\"]*\"|'[^']*'|\S+)|-c\s+\S+|--(?:git-dir|work-tree|namespace)(?:=|\s+)\S+"
        r"|--[a-z-]+|-[pP]))*\s+")

_RISK_RULES = [
    # 刪除檔案 / 資料
    (r"(?<![\w-])(rm|rmdir|rd|del|erase)(?![\w-])", "刪除檔案或資料", ("shell",)),
    (r"\b(Remove-Item|Clear-Content|Clear-RecycleBin)\b", "刪除檔案或資料", _BOTH),
    (r"\b(shutil\.rmtree|os\.remove|os\.unlink|os\.rmdir|os\.removedirs)\b|\.unlink\(|\.rmdir\(", "刪除檔案或資料", ("python",)),
    # 磁碟
    (r"\b(Format-Volume|Clear-Disk|Initialize-Disk|diskpart)\b|\bformat\s+[a-z]:", "格式化或變更磁碟", _BOTH),
    # 關機 / 重開 / 登出
    (r"\b(Stop-Computer|Restart-Computer|shutdown|logoff)\b", "關機、重新開機或登出", _BOTH),
    # 結束程序 / 服務
    (r"\b(Stop-Process|taskkill|Stop-Service|Set-Service)\b", "結束程式或停止服務", _BOTH),
    (r"\bkill\s+-?\d", "結束程式或停止服務", ("shell",)),
    # 系統設定 / 登錄檔 / 權限
    (r"\b(Set-ExecutionPolicy|Set-ItemProperty|New-ItemProperty|Remove-ItemProperty|icacls|takeown|bcdedit)\b"
     r"|\breg\s+(add|delete|import)\b|\bwinreg\.(SetValue|DeleteKey|DeleteValue)", "變更系統設定、登錄檔或權限", _BOTH),
    # 網路 / 防火牆
    (r"\b(netsh|New-NetFirewallRule|Set-NetFirewallProfile|Disable-NetAdapter)\b", "變更網路或防火牆設定", _BOTH),
    # 以系統管理員身分執行、動態執行下載的程式碼
    (r"-Verb\s+RunAs|\b(Invoke-Expression|iex)\b", "以系統管理員身分或動態執行程式碼", _BOTH),
    # Git 提交 / 不可逆 / 對外推送。commit 也列入：使用者規則「核心程式碼只能在使用者確認後才 commit」，
    # RD 或 AI 自行 commit 時會被攔下詢問 (Claude 由 hook、agy 由即時中斷、Gemini 由同意卡片)
    (_GIT + r"(commit|push|reset\s+--hard|clean\s+-[a-z]*f|branch\s+-D|checkout\s+--\s)", "Git 提交、推送或不可逆操作", _BOTH),
    # 對外寄信
    (r"\b(Send-MailMessage|smtplib)\b", "對外寄送郵件", _BOTH),
    # 修改 MCP 設定檔與核可紀錄：它們決定 ArQ 會啟動哪些外部程式、哪些工具免同意。
    # 只擋設定檔不夠：同時改寫 _temp/mcp_approved.json 就能繞過指紋核可。範例檔 mcp_servers.example.json 不在此限
    (r"\bmcp_(servers|approved)\.json\b", "修改 MCP 設定檔或核可紀錄", _BOTH),
    # 修改 PM 系統提示詞：避免外部內容誘導 PM 改寫自己的規則。讀檔指令也會觸發 (多一張同意卡)，可接受
    (r"\bprompts[\\/][\w.-]+\.md\b", "修改 PM 系統提示詞", _BOTH),
    # 對話記憶：被注入的內容寫進去會跨重啟永久留在 PM 的記憶裡
    (r"\bchat_history\b|\bpending_reports\.json\b", "修改 ArQ 對話記憶", _BOTH),
    # 存取機密檔 (.env、Google 服務帳號憑證、SSH 私鑰)：避免金鑰經由輸出外洩 (另見 tools/secret_guard.py)
    (r"\.env\b(?!\.example)|\bcredentials\.json\b|\bid_(rsa|ed25519|ecdsa)\b", "存取機密設定檔", _BOTH),
]
_RISK_RES = [(re.compile(p, re.IGNORECASE), reason, scopes) for p, reason, scopes in _RISK_RULES]

INSTALL_REASON = "安裝新套件"

# -------------------------------------------------------------
# 3. 合併 PR / 分支：一律需要同意，而且必須「單獨一張卡」
#    事件：merge 夾帶在含 commit / push 的同一段指令裡，只跳一張「Git 提交、推送」卡就過關。
#    因此 merge 不只要同意，還不得與其他指令同段執行 (見 merge_violation，agent.py 會直接拒絕)。
# -------------------------------------------------------------
MERGE_REASON = "合併到 main"

_MERGE_PATTERNS = [
    r"\bpr\s+merge\b",                                     # gh pr merge (含 & 'C:\...\gh.exe' pr merge)
    _GIT + r"merge(?![\w-])",                              # git [全域選項] merge (不含 git merge-base)
    r"/pulls/\d+/merge\b",                                 # gh api / REST 的 merge 端點
    r"['\"]pr['\"]\s*,\s*['\"]merge['\"]",                 # subprocess.run(["gh", "pr", "merge", ...])
    r"['\"]git['\"]\s*,\s*['\"]merge['\"]",                # subprocess.run(["git", "merge", ...])
]
_MERGE_RE = re.compile("|".join(_MERGE_PATTERNS), re.IGNORECASE)
_PR_NUMBER_RES = [
    re.compile(r"\bpr\s+merge\s+(?:https?://\S+/pull/)?#?(\d+)\b", re.IGNORECASE),
    re.compile(r"/pulls?/(\d+)(?:/merge)?\b", re.IGNORECASE),
]
# 指令分隔：; && || | 換行 (PowerShell 與 Bash 共通)
_COMMAND_SEPARATOR_RE = re.compile(r"&&|\|\||[;|\r\n]")
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")


def looks_like_merge(text: str, kind: str = "shell") -> bool:
    """
    判斷一段指令或程式碼是否包含合併 PR / 分支的動作。
    shell 指令先去掉引號內的文字，避免 commit 訊息「pr merge 守門」被誤判；Python 程式碼的指令常寫在字串裡，不去掉。
    """
    if not text:
        return False
    return bool(_MERGE_RE.search(_QUOTED_RE.sub("''", text) if kind == "shell" else text))


def extract_pr_number(text: str) -> Optional[int]:
    """從 merge 指令取出 PR 編號 (gh pr merge 28、.../pull/28、pulls/28/merge)；git merge 分支則回傳 None"""
    for pattern in _PR_NUMBER_RES:
        m = pattern.search(text or "")
        if m:
            return int(m.group(1))
    return None


def merge_violation(text: str, kind: str = "shell") -> Optional[str]:
    """
    merge 必須單獨執行：回傳拒絕理由 (給 Gemini 看)；None = 沒有 merge，或 merge 是唯一的指令。
    Python 程式碼一律不得 merge (看不清楚實際指令)，請改用 run_powershell_sync 單獨執行。
    """
    if not looks_like_merge(text, kind):
        return None
    if kind == "python":
        return "合併到 main 不得寫在 Python 程式碼裡。請改用 run_powershell_sync，單獨執行一行 merge 指令（例如 gh pr merge <編號>）。"
    commands = [c for c in _COMMAND_SEPARATOR_RE.split(_QUOTED_RE.sub("''", text)) if c.strip()]
    if len(commands) > 1:
        return ("合併到 main 必須單獨執行，系統已拒絕這段指令（沒有執行任何部分）。"
                "請先執行其他步驟，再另外用一次 run_powershell_sync 只執行 merge（例如 gh pr merge <編號>）。")
    return None


def looks_like_install(text: str) -> bool:
    """判斷一段指令或程式碼是否包含安裝套件的動作"""
    return bool(text) and bool(_INSTALL_RE.search(text))


# -------------------------------------------------------------
# 3b. PM 不得自己讀程式碼：規則 7 只靠提示詞時 Gemini 仍會自己讀檔研究 (又慢又耗 Token)，
#     改由程式攔截，提示改派 plan=true 只讀規劃。只用於 Gemini 的工具；RD 不受影響。
#     寧可少擋：只攔「讀取程式碼檔內容」與 git diff/show/grep/blame；task/ 下的檔案與非程式碼檔照常可讀。
# -------------------------------------------------------------
CODE_READ_REASON = ("PM 不得自己讀程式碼研究實作（規則 7）。需要了解現有程式時，請改用派工工具加 plan=true，"
                    "讓工程師只讀研究並回傳規格；task/ 下的檔案與 log、CSV、文件等非程式碼檔可以照常讀。")
_CODE_EXT = r"(?:pyw?|ps1|psm1|bat|cmd|js|ts|jsx|tsx)"
_CODE_PATH_RE = re.compile(rf"[\w.:/\\*~-]*\.{_CODE_EXT}(?![\w])", re.IGNORECASE)
_SHELL_READ_RE = re.compile(r"(?<![\w-])(get-content|gc|cat|type|more|select-string|sls|findstr|grep|head|tail)(?![\w-])",
                            re.IGNORECASE)
_LIST_PIPE_READ_RE = re.compile(r"(?<![\w-])(get-childitem|gci|ls|dir)(?![\w-])([^|]*)\|\s*(get-content|gc|cat|type|select-string|sls)\b",
                                re.IGNORECASE)
_GIT_READ_RE = re.compile(_GIT + r"(diff|show|grep|blame)\b|['\"]git['\"]\s*,\s*['\"](diff|show|grep|blame)['\"]",
                          re.IGNORECASE)
_PY_READ_RE = re.compile(r"\b(open|read_text|read_bytes|readlines)\s*\(|\binspect\.(getsource|getsourcelines|getfile)\b")


def _in_task_dir(path: str) -> bool:
    return bool(re.search(r"(^|/)task/", path.replace("\\", "/").lower()))


def _reads_project_code(text: str) -> bool:
    """文字中有 task/ 以外的程式碼檔路徑"""
    return any(not _in_task_dir(p) for p in _CODE_PATH_RE.findall(text or ""))


def path_read_violation(path: str) -> Optional[str]:
    """read_text_file：讀 task/ 以外的程式碼檔時回傳拒絕理由"""
    return CODE_READ_REASON if _reads_project_code(str(path or "")) else None


def code_read_violation(text: str, kind: str = "shell") -> Optional[str]:
    """PowerShell / Python：讀取專案程式碼內容 (或 git diff/show/grep/blame) 時回傳拒絕理由；None = 放行"""
    if not text:
        return None
    if _GIT_READ_RE.search(text):
        return CODE_READ_REASON
    if kind == "python":
        if re.search(r"\binspect\.(getsource|getsourcelines|getfile)\b", text):
            return CODE_READ_REASON
        return CODE_READ_REASON if _PY_READ_RE.search(text) and _reads_project_code(text) else None
    unquoted = _QUOTED_RE.sub("''", text)   # 讀檔指令要在引號外 (commit 訊息裡的 cat、bot.py 不算)
    for m in _LIST_PIPE_READ_RE.finditer(unquoted):
        if not _in_task_dir(m.group(2)):
            return CODE_READ_REASON
    return CODE_READ_REASON if _SHELL_READ_RE.search(unquoted) and _reads_project_code(text) else None


def classify_risk(text: str, kind: str = "shell") -> Optional[str]:
    """
    判斷指令 / 程式碼是否需要使用者同意。
    kind："shell" (PowerShell / Bash 指令) 或 "python" (Python 程式碼)。
    回傳理由字串 (可能以「、」串接多個)；None = 無風險，可直接執行。
    """
    if not text:
        return None
    reasons: List[str] = []
    if looks_like_install(text):
        reasons.append(INSTALL_REASON)
    if looks_like_merge(text, kind):
        reasons.append(MERGE_REASON)
    for pattern, reason, scopes in _RISK_RES:
        if kind in scopes and reason not in reasons and pattern.search(text):
            reasons.append(reason)
    return "、".join(reasons) if reasons else None


# -------------------------------------------------------------
# 4. 事後套件比對 (保險機制)
# -------------------------------------------------------------

def snapshot_packages() -> Dict[str, str]:
    """取得目前 Python 環境 (ArQ 的 .venv) 已安裝套件 {名稱小寫: 版本}"""
    packages: Dict[str, str] = {}
    for dist in metadata.distributions():
        name = dist.metadata.get("Name")
        if name:
            packages[name.lower()] = dist.version
    return packages


def diff_packages(before: Dict[str, str], after: Dict[str, str]) -> List[str]:
    """比對兩次快照，回傳人類可讀的變更清單"""
    changes: List[str] = []
    for name in sorted(after):
        if name not in before:
            changes.append(f"➕ 新增 `{name}` {after[name]}")
        elif before[name] != after[name]:
            changes.append(f"🔄 更新 `{name}` {before[name]} → {after[name]}")
    for name in sorted(before):
        if name not in after:
            changes.append(f"➖ 移除 `{name}` {before[name]}")
    return changes


@contextmanager
def report_new_packages(notify: Optional[Callable[[str], None]], source: str) -> Iterator[None]:
    """
    包住一段執行過程；結束後若 ArQ 環境的套件有變動，呼叫 notify 通知使用者。
    正常情況下安裝都會先經過 classify_risk() 取得同意；這裡是抓「寫成腳本間接安裝」等漏網之魚。
    限制：只看得到 ArQ 自己的 Python 環境，系統 Python / npm 全域的安裝只能靠指令比對。
    notify 會在「目前執行緒」被呼叫。
    """
    before = snapshot_packages()
    try:
        yield
    finally:
        changes = diff_packages(before, snapshot_packages())
        if changes and notify:
            notify(f"📦 **{source} 變更了 ArQ 環境的套件**（未經事前比對攔截，請留意）：\n" + "\n".join(changes))
