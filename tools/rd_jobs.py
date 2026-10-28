"""
執行中 RD 派工的登記表與強制停止

使用者規則 (修改時必須維持)：
    1. 使用者可在 RD 開發途中隨時停止 (派工訊息的 🛑 按鈕或 /stop)。
    2. 停止會結束整個程序樹 (taskkill /T /F，含 RD 啟動的子程序)，結果標為「已取消」，
       不觸發 failover (不會改派給另一位工程師)。
    3. RD 正在等待使用者同意時也能停止：等待中的同意視為拒絕並立即返回 (cancellable_approve)。
    4. 停止不自動還原改動；由 rd_dispatch 回報 git 狀態，交給使用者決定。

用法：
    rd_dispatch.run_dev_task 為每次派工建立 DevJob 並 register()；引擎啟動子程序後 job.attach(pid)，
    結束時 detach(pid)；每個階段檢查 job.cancelled。bot.py 以 set_announcer() 取得新派工，
    在派工訊息附上停止按鈕；/stop 以 list_jobs() / cancel_job() 操作。

ORIGIN (contextvars)：bot.py 在呼叫 agent.chat 前設定 {"owner_id", "channel_id"}；
asyncio.to_thread 會複製 context，所以 Gemini 派工 (在工具執行緒中) 建立的 DevJob 也知道該回報到哪個頻道。
"""

import contextvars
import json
import logging
import os
import subprocess
import threading
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from tools.config import temp_path

logger = logging.getLogger(__name__)

ApproveFn = Callable[[str, str], bool]

# 派工來源 (Discord 使用者與頻道)；未設定時為 None
ORIGIN: contextvars.ContextVar = contextvars.ContextVar("rd_job_origin", default=None)

# cancellable_approve 檢查停止的間隔秒數
_APPROVE_POLL_SECONDS = 0.2


def _kill_tree(pid: int) -> None:
    """強制終止整個程序樹 (測試時 patch 這個函式)"""
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, stdin=subprocess.DEVNULL)


def _first_line(text: str, limit: int = 80) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line:
            return line if len(line) <= limit else line[:limit] + "…"
    return "（無描述）"


class DevJob:
    """一次派工 (可能經過 failover 換引擎)。所有方法皆執行緒安全。"""

    def __init__(self, task: str, engine_label: str, scope: str, ticket: Optional[int] = None,
                 owner_id: Optional[int] = None, channel_id: Optional[int] = None):
        origin = ORIGIN.get() or {}
        self.id = uuid.uuid4().hex[:8]
        self.task = task or ""
        self.engine_label = engine_label
        self.scope = scope
        self.ticket = ticket
        self.owner_id = owner_id if owner_id is not None else origin.get("owner_id")
        self.channel_id = channel_id if channel_id is not None else origin.get("channel_id")
        # 原頻道物件：DM 頻道不在 discord.py 快取中，只靠 ID 用 get_channel 會找不到
        self.channel: Any = origin.get("channel")
        self.started_at = time.time()
        self.cancelled_by = ""
        self.cancelled_at: Optional[float] = None
        self._cancel = threading.Event()
        self._lock = threading.Lock()
        self._pids: set = set()
        self._finish_callbacks: List[Callable[[], None]] = []
        # 目前佔用的引擎 ("claude" / "antigravity")；由 reserve 設定、換人時更新、finish 釋放
        self.engine: Optional[str] = None

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def attach(self, pid: int) -> None:
        """登記引擎啟動的程序；已停止時立即結束它 (停止與啟動幾乎同時發生的情況)"""
        with self._lock:
            if not self._cancel.is_set():
                self._pids.add(pid)
                return
        _kill_tree(pid)

    def detach(self, pid: int) -> None:
        with self._lock:
            self._pids.discard(pid)

    def cancel(self, by: str = "") -> bool:
        """停止派工並結束所有已登記的程序樹；回傳 False 代表先前已停止"""
        with self._lock:
            if self._cancel.is_set():
                return False
            self._cancel.set()
            self.cancelled_by = by
            self.cancelled_at = time.time()
            pids = list(self._pids)
        for pid in pids:
            try:
                _kill_tree(pid)
            except Exception as e:
                logger.warning(f"⚠️ 結束程序 {pid} 失敗: {e}", exc_info=True)
        return True

    def wait_cancelled(self, timeout: float) -> bool:
        return self._cancel.wait(timeout)

    def add_finish_callback(self, fn: Callable[[], None]) -> None:
        with self._lock:
            self._finish_callbacks.append(fn)

    def summary(self) -> Dict[str, Any]:
        return {
            "id": self.id, "engine_label": self.engine_label, "task": _first_line(self.task),
            "ticket": self.ticket, "scope": self.scope, "cancelled": self.cancelled,
            "elapsed_seconds": int(time.time() - self.started_at),
        }


_JOBS: Dict[str, DevJob] = {}
_REGISTRY_LOCK = threading.Lock()
_ANNOUNCER: Optional[Callable[[DevJob], None]] = None


def set_announcer(fn: Optional[Callable[[DevJob], None]]) -> None:
    """bot.py 設定：新派工登記時呼叫 (在背景執行緒中)，用來送出附停止按鈕的訊息"""
    global _ANNOUNCER
    _ANNOUNCER = fn


# -------------------------------------------------------------
# 重啟時告知中斷的任務：背景派工的執行緒會隨 ArQ 結束，登記表同步寫到檔案，
# 下次開機讀到殘留的項目就代表那些任務被中斷了 (bot.on_ready 回報給使用者)
# -------------------------------------------------------------

_DEFAULT_STATE_PATH = temp_path("rd_jobs_running.json")
STATE_PATH = _DEFAULT_STATE_PATH


def _persist_locked() -> None:
    """呼叫端需持有 _REGISTRY_LOCK。單元測試 (ARQ_TESTING) 不寫真正的狀態檔，避免開機時誤報"""
    if os.getenv("ARQ_TESTING") and STATE_PATH == _DEFAULT_STATE_PATH:
        return
    entries = [{"id": j.id, "ticket": j.ticket, "engine_label": j.engine_label, "task": _first_line(j.task),
                "channel_id": j.channel_id, "owner_id": j.owner_id, "started_at": j.started_at}
               for j in _JOBS.values()]
    try:
        if entries:
            os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
            with open(STATE_PATH, "w", encoding="utf-8") as f:
                json.dump(entries, f, ensure_ascii=False)
        elif os.path.exists(STATE_PATH):
            os.remove(STATE_PATH)
    except OSError as e:
        logger.warning(f"⚠️ 寫入派工狀態檔失敗: {e}", exc_info=True)


def take_interrupted() -> List[Dict[str, Any]]:
    """開機時呼叫：取出上次結束時還在執行的派工 (讀完即刪除，只回報一次)"""
    try:
        with open(STATE_PATH, encoding="utf-8") as f:
            entries = json.load(f)
        os.remove(STATE_PATH)
    except (OSError, ValueError):
        return []
    return entries if isinstance(entries, list) else []


def format_interrupted(entries: List[Dict[str, Any]]) -> str:
    from tools.rd_common import job_tag
    lines = [f"⚠️ ArQ 重啟前有 {len(entries)} 個 RD 任務被中斷（改動保留在各自的 worktree，"
             "可請 PM 帶 session_id 接續或重新派工）："]
    lines += [f"• 【{job_tag(e.get('ticket'), e.get('engine_label') or '')}】{e.get('task', '')}" for e in entries]
    return "\n".join(lines)


def register(job: DevJob, announce: bool = False) -> None:
    with _REGISTRY_LOCK:
        _JOBS[job.id] = job
        _persist_locked()
    if announce and _ANNOUNCER is not None:
        try:
            _ANNOUNCER(job)
        except Exception as e:
            logger.warning(f"⚠️ 送出派工停止按鈕失敗: {e}", exc_info=True)


# -------------------------------------------------------------
# 並行上限與引擎占用 (拆單 34-1)：同時執行的 RD 任務有上限、每個引擎同時只跑一個、
# 同一張單不會同時有兩位 RD。檢查與佔用在同一把鎖內完成，兩個派工同時搶也只有一個成功。
# -------------------------------------------------------------

MAX_PARALLEL = 2  # 預設上限 (Claude 一個、agy 一個)；RD_MAX_PARALLEL 可覆寫
_ENGINE_NAMES = {"claude": "Claude Code", "antigravity": "Antigravity"}


def max_parallel() -> int:
    try:
        value = int(os.getenv("RD_MAX_PARALLEL", "").strip() or MAX_PARALLEL)
    except ValueError:
        return MAX_PARALLEL
    return max(1, value)


def _ticket_ref(job: DevJob) -> str:
    return f"#{job.ticket}" if job.ticket else "另一個任務"


def reserve(job: DevJob, engine: str) -> Optional[str]:
    """
    讓 job 佔用 engine；成功回傳 None，否則回傳拒絕理由 (給使用者與 PM 看)。
    job 原本佔著別的引擎 (接手換人) 時先釋放。只計入已登記 (register) 的派工。
    """
    name = _ENGINE_NAMES.get(engine, engine)
    with _REGISTRY_LOCK:
        others = [j for j in _JOBS.values() if j is not job and j.engine]
        if job.ticket:
            for other in _JOBS.values():
                if other is not job and other.ticket == job.ticket:
                    return f"#{job.ticket} 已有 RD（{other.engine_label}）在做，同一張單不能同時派給兩位 RD。"
        for other in others:
            if other.engine == engine:
                return f"{name} 正在做 {_ticket_ref(other)}，無法同時再接一個任務。"
        limit = max_parallel()
        if len(others) >= limit:
            busy = "、".join(_ticket_ref(o) for o in others)
            return f"已有 {len(others)} 個 RD 任務在執行（{busy}），達到同時執行上限 {limit}。"
        job.engine = engine
    return None


def engine_holder(engine: str, exclude: Optional[DevJob] = None) -> Optional[DevJob]:
    """正在佔用 engine 的其他派工；沒有回傳 None (用來分辨「引擎忙碌」與上限 / 同單衝突)"""
    with _REGISTRY_LOCK:
        for j in _JOBS.values():
            if j is not exclude and j.engine == engine:
                return j
    return None


def release(job: DevJob) -> None:
    with _REGISTRY_LOCK:
        job.engine = None


def busy_engines() -> set:
    with _REGISTRY_LOCK:
        return {j.engine for j in _JOBS.values() if j.engine}


def running_tickets() -> set:
    with _REGISTRY_LOCK:
        return {j.ticket for j in _JOBS.values() if j.engine and j.ticket}


def finish(job: DevJob) -> None:
    """派工結束：移出登記表、釋放引擎並執行結束回呼 (例如停用停止按鈕)"""
    with _REGISTRY_LOCK:
        _JOBS.pop(job.id, None)
        job.engine = None
        _persist_locked()
    with job._lock:
        callbacks = list(job._finish_callbacks)
    for fn in callbacks:
        try:
            fn()
        except Exception as e:
            logger.warning(f"⚠️ 派工結束回呼失敗: {e}", exc_info=True)


def get(job_id: str) -> Optional[DevJob]:
    with _REGISTRY_LOCK:
        return _JOBS.get(job_id)


def list_jobs() -> List[DevJob]:
    with _REGISTRY_LOCK:
        return sorted(_JOBS.values(), key=lambda j: j.started_at)


def cancel_job(job_id: str, by: str = "") -> Dict[str, Any]:
    """依 ID 停止派工 (/stop 與停止按鈕共用)；回傳 {"success", "message"}"""
    job = get(job_id)
    if job is None:
        return {"success": False, "message": "這個派工已經結束了。"}
    if not job.cancel(by=by):
        return {"success": False, "message": f"{job.engine_label} 的派工已在停止中。"}
    return {"success": True, "message": f"🛑 已停止 {job.engine_label} 的開發，正在整理停止前的狀態…"}


def cancellable_approve(approve: Optional[ApproveFn], job: DevJob) -> ApproveFn:
    """
    包裝同意函式：等待使用者決定期間若派工被停止，立即視為拒絕並返回 (Discord 上的卡片之後會自行逾時)。
    approve 為 None 時一律拒絕 (fail-closed)。
    """
    def wrapped(title: str, detail: str) -> bool:
        if approve is None or job.cancelled:
            return False
        box: Dict[str, Any] = {}
        done = threading.Event()

        def ask() -> None:
            try:
                box["ok"] = bool(approve(title, detail))
            except Exception as e:
                logger.warning(f"⚠️ 詢問使用者同意失敗，視為拒絕: {e}", exc_info=True)
                box["ok"] = False
            finally:
                done.set()

        threading.Thread(target=ask, daemon=True).start()
        while not done.wait(_APPROVE_POLL_SECONDS):
            if job.cancelled:
                return False
        return bool(box.get("ok")) and not job.cancelled

    return wrapped


def _reset_for_tests(keep_state: bool = False) -> None:
    """keep_state=True：只清記憶體、不動狀態檔 (模擬程式被結束)"""
    with _REGISTRY_LOCK:
        _JOBS.clear()
        if not keep_state:
            _persist_locked()
    set_announcer(None)
