"""
兩位 RD 工程師 (Claude Code / Antigravity) 共用的設定：工作範圍 (scope)、派工 prompt、核心修改驗收佐證

scope (工作範圍)：
    "task"：一般任務，工作目錄 task/ (不進版控)。預設值。
    "core"：修改 ArQ 本身 (agent.py、bot.py、tools/…)，工作目錄為專案根目錄。
            經 Gemini 派工時需使用者同意 (agent.py 的 _dispatch_risk)；完成後由 collect_acceptance()
            自動產生驗收佐證 (git diff --stat + 全部單元測試)，由使用者確認後才 commit。
            RD 不得自行 commit：git commit 已列入 risk_policy，執行時會被攔下詢問使用者。
"""

import logging
import os
import re
import subprocess
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from tools.config import PROJECT_ROOT, env_float
from tools.secret_guard import child_env, redact

logger = logging.getLogger(__name__)

SCOPE_TASK = "task"
SCOPE_CORE = "core"
SCOPES = {SCOPE_TASK, SCOPE_CORE}

CLAUDE = "claude"
ANTIGRAVITY = "antigravity"

ENGINE_LABELS = {CLAUDE: "Claude Code", ANTIGRAVITY: "Antigravity"}

NotifyFn = Callable[[str], None]
ApproveFn = Callable[[str, str], bool]


def _safe_notify(notify: Optional[NotifyFn], text: str) -> None:
    if notify is None:
        return
    try:
        notify(text)
    except Exception as e:
        logger.warning(f"⚠️ 發送通知失敗: {e}", exc_info=True)


TEST_TIMEOUT = 300

# -------------------------------------------------------------
# RD 逾時：看「多久沒動靜」，總時長只是寬鬆的最後保險
# 曾以 900 秒總時長判斷： 的 Claude 一直有進展、只差收尾就被砍掉並改派 agy 從頭做。
# 閒置上限要大於單一長指令 (Claude 的 Bash 工具最長 10 分鐘，期間不會有事件)。
# -------------------------------------------------------------

DEFAULT_IDLE_TIMEOUT = 600
DEFAULT_TOTAL_TIMEOUT = 3600
TIMEOUT_IDLE = "idle"
TIMEOUT_TOTAL = "total"


def _positive_seconds(name: str, default: float) -> float:
    return env_float(name, default, minimum=1e-9)


def rd_timeouts(prefix: str) -> Tuple[float, float]:
    """(閒置上限, 總時長上限) 秒數：<prefix>_IDLE_TIMEOUT / <prefix>_TIMEOUT，無效值用預設；閒置不超過總時長"""
    idle = _positive_seconds(f"{prefix}_IDLE_TIMEOUT", DEFAULT_IDLE_TIMEOUT)
    total = _positive_seconds(f"{prefix}_TIMEOUT", DEFAULT_TOTAL_TIMEOUT)
    return min(idle, total), total


def timeout_message(engine_label: str, kind: str, idle: float, total: float) -> str:
    if kind == TIMEOUT_IDLE:
        return f"{engine_label} 連續 {idle:g} 秒沒有任何動靜，已強制中止"
    return f"{engine_label} 開發超過總時長上限 {total:g} 秒，已強制中止"


class Watchdog:
    """
    RD 程序的逾時監看：連續 idle 秒沒有 touch() → 觸發 "idle"；累計執行超過 total 秒 → 觸發 "total"。
    pause() 到 resume() 之間 (等使用者同意) 兩者都不計時。只觸發一次，on_timeout(kind) 在背景執行緒呼叫。
    progressed：曾有「有意義的事件」(touch(progress=True))，用來判斷逾時前是否已有進展。
    """

    def __init__(self, idle: float, total: float, on_timeout: Callable[[str], None],
                 interval: Optional[float] = None, clock: Callable[[], float] = time.monotonic):
        self.idle, self.total = idle, total
        self._on_timeout = on_timeout
        # 檢查間隔：一般 1 秒；上限設得很小 (測試) 時跟著縮短
        self._interval = interval or max(0.01, min(1.0, idle / 10))
        self._clock = clock
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._paused = False
        self._last = clock()
        self._active = 0.0        # 累計執行時間 (不含暫停)
        self._quiet = 0.0         # 上次 touch 之後的執行時間 (不含暫停)
        self.fired: Optional[str] = None
        self.progressed = False

    def start(self) -> None:
        threading.Thread(target=self._loop, name="rd-watchdog", daemon=True).start()

    def _advance(self) -> None:
        """呼叫端需持有 _lock：把上次結算到現在的時間計入 (暫停中不計)"""
        now = self._clock()
        if not self._paused:
            elapsed = now - self._last
            self._active += elapsed
            self._quiet += elapsed
        self._last = now

    def touch(self, progress: bool = False) -> None:
        with self._lock:
            self._advance()
            self._quiet = 0.0
            if progress:
                self.progressed = True

    def pause(self) -> None:
        with self._lock:
            self._advance()
            self._paused = True

    def resume(self) -> None:
        with self._lock:
            self._advance()
            self._paused = False

    def cancel(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.wait(self._interval):
            with self._lock:
                self._advance()
                kind = (TIMEOUT_TOTAL if self._active >= self.total
                        else TIMEOUT_IDLE if self._quiet >= self.idle else None)
                if kind:
                    self.fired = kind
            if kind:
                self._stop.set()
                self._on_timeout(kind)
                return


def workspace_for(scope: str, workdir: str = "") -> str:
    """scope 對應的 RD 工作目錄；core 有 workdir (該單的 worktree) 時改在 worktree"""
    if scope == SCOPE_CORE:
        return workdir or PROJECT_ROOT
    return os.path.join(PROJECT_ROOT, "task")


def job_tag(ticket: Any, engine_label: str) -> str:
    """通知前綴：「#77｜Claude Code」；沒有單號時只有 RD 名稱。同時有多位 RD 時分得出是誰、哪張單"""
    label = engine_label or "RD 工程師"
    return f"#{ticket}｜{label}" if ticket else label


def workdir_env(workdir: str) -> Dict[str, str]:
    """
    在 worktree 開發時給 RD 程序的環境變數：uv 沿用 ArQ 的 .venv，不在每個 worktree 另建虛擬環境
    (另建會重新安裝全部套件；改動套件仍由安裝守門攔下)。不在 worktree 時回傳空 dict。
    """
    if not workdir or os.path.normcase(os.path.abspath(workdir)) == os.path.normcase(PROJECT_ROOT):
        return {}
    return {"UV_PROJECT_ENVIRONMENT": os.path.join(PROJECT_ROOT, ".venv")}


# Sub agent 規則：RD 在一次派工內自行判斷要不要平行處理
SUBAGENT_RULE = (
    "7. 可以自行開 sub agent 平行處理互相獨立的子任務（例如查程式碼、寫測試、實作不同檔案），縮短開發時間；"
    "有相依（後一步需要前一步的結果）或會改到同一批檔案的部分必須依序處理。"
    "上面的開發規範（安裝、git commit / push、TDD、工作範圍）對 sub agent 一樣適用，派給 sub agent 時要把相關規則一併交代。"
    "若你的環境不支援 sub agent，照常依序完成即可，不算失敗。"
    "回報最後加一段「Sub agent：」，每個一行列出 `- 類型：負責內容`；沒有開就寫「Sub agent：無」。"
)

# 只讀規劃版的 sub agent 規則：研究可以分頭進行；回報段落讓回顧量得到 的效果
PLAN_SUBAGENT_RULE = (
    "4. 研究範圍大時可以開 sub agent 平行研究互相獨立的部分（例如分頭讀不同模組）；sub agent 一樣只讀，"
    "上面的規則對它們一樣適用。若你的環境不支援 sub agent，照常依序研究即可。"
    "回覆最後加一段「Sub agent：」，每個一行列出 `- 類型：負責內容`；沒有開就寫「Sub agent：無」。"
)

# Claude Code 開 sub agent 的工具名稱 (新版為 Agent，舊版為 Task)
SUBAGENT_TOOLS = ("Agent", "Task")
_SUBAGENT_HEADING = re.compile(r"^[\s#>*_-]*sub[\s-]?agents?[*_\s]*[：:][*_\s]*(.*)$", re.IGNORECASE)
_BULLET = re.compile(r"^\s*(?:[-*•]|\d+[.)、])\s+(.*\S)\s*$")
_NONE_WORDS = ("無", "沒有", "none", "n/a", "0 個", "未使用")


def extract_subagents(event: Dict[str, Any]) -> List[str]:
    """從 Claude Code stream-json 的 assistant 事件取出開出的 sub agent，回傳 ["類型：負責內容"]"""
    if event.get("type") != "assistant":
        return []
    found = []
    for block in (event.get("message") or {}).get("content") or []:
        if isinstance(block, dict) and block.get("type") == "tool_use" and block.get("name") in SUBAGENT_TOOLS:
            data = block.get("input") or {}
            kind = str(data.get("subagent_type") or "general-purpose").strip()
            desc = str(data.get("description") or "").strip() or "（未說明）"
            found.append(f"{kind}：{desc}")
    return found


def parse_subagent_report(text: str) -> List[str]:
    """解析 RD 回報的「Sub agent：」段落 (引擎沒有事件可抓時使用，例如 Antigravity)；沒有或寫「無」時回傳 []"""
    lines = str(text or "").splitlines()
    for i, line in enumerate(lines):
        m = _SUBAGENT_HEADING.match(line)
        if not m:
            continue
        inline = m.group(1).strip().strip("*_ ").strip()
        if inline:
            return [] if inline.lower().startswith(_NONE_WORDS) else [inline]
        items = []
        for nxt in lines[i + 1:]:
            b = _BULLET.match(nxt)
            if not b:
                if items or nxt.strip():
                    break
                continue
            items.append(b.group(1).strip())
        return [] if len(items) == 1 and items[0].lower().startswith(_NONE_WORDS) else items
    return []


def build_pm_prompt(task_description: str, scope: str, install_rule: str) -> str:
    """
    組出新任務的 PM 結構化派工 prompt。
    install_rule：各引擎的安裝套件規則 (Claude 有 hook 會暫停詢問；agy 只能自行提出請求)。
    """
    if scope == SCOPE_CORE:
        place = (
            "1. 你目前在 ArQ 專案根目錄，這次任務是修改 ArQ 本身。動工前先閱讀 GEMINI.md，"
            "遵守其中的使用者規則與資安不變量；不得修改或讀取 .env。\n"
            "2. 改動要精準、只動必要的檔案；新增或修改功能時同步更新 tests/ 與相關文件。\n"
        )
        verify = (
            "4. 遵守 TDD：先寫或更新測試，再實作，最後執行 `uv run python -m unittest discover -s tests` 全數通過。\n"
            "5. 不得執行 git commit / push（由使用者確認後才提交）。回報時列出修改的檔案、測試結果與需要使用者決定的事項。"
        )
    else:
        place = (
            "1. 你目前的工作目錄就是任務區，程式碼直接存放於目前目錄（可自行建立子資料夾）。\n"
            "2. 成果物（報表、簡報、圖表、CSV 等）一律輸出至目前目錄下的 `artifact/`。\n"
        )
        verify = (
            "4. 遵守 TDD：先寫測試再實作，完成後執行測試並附上通過佐證；不得執行 git push。\n"
            "5. 回報開發成果、修改的檔案清單與安裝的套件。"
        )
    return (
        "【ArQ 專案經理 (PM) 任務指派】\n"
        f"需求說明：\n{task_description}\n\n"
        "【開發規範】：\n"
        + place
        + "3. 一般步驟自主完成，不需等待確認。" + install_rule + "\n"
        + verify + "\n"
        "6. 若有需求不清楚或需要使用者做決定的地方，請在回報中明確列出問題（附你的建議），不要自行假設重大決策。\n"
        + SUBAGENT_RULE
    )


def build_plan_prompt(task_description: str, scope: str) -> str:
    """
    只讀規劃的 prompt：PM 需求對焦時請 RD 研究現有程式並提出規格，不得修改任何檔案。
    Claude 另以 --permission-mode plan 強制唯讀；agy 只能靠這段約束，結束後由程式檢查工作目錄。
    """
    where = ("ArQ 專案根目錄（研究 ArQ 本身；先看 GEMINI.md；不得讀取 .env）" if scope == SCOPE_CORE
             else "任務區（目前目錄）")
    return (
        "【ArQ PM 只讀規劃請求】\n"
        f"要研究的需求：\n{task_description}\n\n"
        "【規則】\n"
        f"1. 你在{where}。這次只讀：可以閱讀檔案、搜尋程式碼，不得修改、新增或刪除任何檔案，"
        "不得執行會改變狀態的指令（安裝、git、寫檔）。\n"
        "2. 研究後直接回覆建議規格（Markdown，會原樣留言到 ticket；精簡講重點，全文約 1,500 字內），包含：\n"
        "   - 目標（一兩句）\n"
        "   - 驗收標準（- [ ] 清單，可測試）\n"
        "   - 影響檔案（每檔一行：檔名與改動，不用表格）\n"
        "   - 做法與風險（只列關鍵決策與主要風險）\n"
        "   - 未決問題（編號，每題附你的建議答案；沒有就寫「無」）\n"
        "3. 不要寫計畫檔，規格直接寫在回覆裡。\n"
        + PLAN_SUBAGENT_RULE
    )


def build_followup_prompt(message: str) -> str:
    """接續既有工程師對話時的訊息 (使用者對工程師提問的裁決、補充規格等)"""
    return (
        "【ArQ PM 補充說明（使用者已回覆你先前的問題）】\n"
        f"{message}\n\n"
        "請依此繼續完成原本的任務，開發規範同前；完成後照原格式回報。"
    )


def _run_in_project(cmd, timeout=60, include_stderr=False, cwd=PROJECT_ROOT):
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, stdin=subprocess.DEVNULL, env=child_env(),
    )
    # git 的 stderr 多是換行符號等警告，不放進驗收卡；unittest 的結果則是寫在 stderr
    out = proc.stdout + (proc.stderr if include_stderr else "")
    return proc.returncode, out.strip()


def collect_worktree_state(cwd: str = PROJECT_ROOT) -> Dict[str, Any]:
    """ArQ 工作目錄的 git 狀態 {"diff_stat", "status"} (已遮蔽)；用於驗收卡與強制停止後的回報"""
    result: Dict[str, Any] = {}
    try:
        _, result["diff_stat"] = _run_in_project(["git", "diff", "--stat"], cwd=cwd)
        _, result["status"] = _run_in_project(["git", "status", "--short"], cwd=cwd)
    except Exception as e:
        result["diff_stat"] = f"(無法取得 git diff：{e})"
        result["status"] = ""
    return {k: redact(v) if isinstance(v, str) else v for k, v in result.items()}


def collect_acceptance(cwd: str = PROJECT_ROOT) -> Dict[str, Any]:
    """
    核心修改 (scope=core) 完成後的驗收佐證：實際跑 git diff 與全部單元測試，不只相信 RD 的自述。
    回傳 {"diff_stat", "status", "tests_passed", "tests_summary"}；輸出皆經過機密遮蔽。
    """
    def run(cmd, **kw):
        return _run_in_project(cmd, cwd=cwd, **kw)
    result: Dict[str, Any] = collect_worktree_state(cwd)
    try:
        code, out = run([sys.executable, "-m", "unittest", "discover", "-s", "tests"],
                        timeout=TEST_TIMEOUT, include_stderr=True)
        lines = [l for l in out.splitlines() if l.startswith(("Ran ", "OK", "FAILED", "ERROR:", "FAIL:"))]
        result["tests_passed"] = code == 0
        result["tests_summary"] = "\n".join(lines[-12:]) or out[-800:]
    except subprocess.TimeoutExpired:
        result["tests_passed"] = False
        result["tests_summary"] = f"單元測試超過 {TEST_TIMEOUT} 秒未完成"
    return {k: redact(v) if isinstance(v, str) else v for k, v in result.items()}
