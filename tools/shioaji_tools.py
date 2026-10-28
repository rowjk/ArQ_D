"""
永豐金證券 Shioaji 唯讀工具 (帳務、損益、報價、排行)

以 Shioaji Python 綁定在 ArQ 行程內直接登入永豐 (不需另外啟動 shioaji HTTP 伺服器，也不依賴其他專案)。
    - shioaji_account_overview()  ：證券交割餘額、證券持倉 (未實現損益)、期貨持倉與保證金
    - shioaji_profit_loss()       ：指定日期區間的已實現損益
    - shioaji_quotes()            ：股票 / 指數 / 期貨即時快照
    - shioaji_rankings()          ：漲跌幅、成交量、成交金額等排行
    - shioaji(action, ...)        ：給 Gemini 的唯一入口，依 action 分派到上面四個函式 (節省工具宣告 Token)

安全設計 (修改時必須維持)：
    1. 只做唯讀查詢：本模組不得呼叫任何下單 / 改單 / 刪單 / 啟用 CA 的 API (tests 會掃描原始碼)。
       金鑰本身也應只勾「行情 / 資料」「帳務」，不勾「交易」。
    2. 金鑰只放 .env 的 SJ_API_KEY / SJ_SEC_KEY (已列入 secret_guard)，不傳給 Gemini、不進 log；
       所有錯誤訊息經 redact() 遮蔽。
    3. 預設模擬環境；.env 設 SJ_PRODUCTION=true 才查正式帳務。
    4. 登入不訂閱委託回報 (subscribe_trade=False)：唯讀金鑰沒有交易權限，訂閱會失敗。

連線：每次登入佔用永豐「每人 5 條」連線額度中的 1 條，因此整個 ArQ 共用一個 session (lazy 登入、加鎖)，
查詢失敗時重置 session，下次呼叫自動重新登入；ArQ 結束時登出。
"""

import atexit
import datetime as dt
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from tools.secret_guard import redact, redact_obj

CONTRACTS_WAIT_SECONDS = 15          # 登入後等待背景合約載入完成的上限 (查報價需要合約)
MAX_QUOTE_CODES = 50                 # 單次報價代碼上限 (官方上限 500，這裡收斂避免灌爆 context)
MAX_RANK_COUNT = 50
MAX_PNL_DAYS = 366

# 常用指數別名 → shioaji 1.7 的指數代碼
_INDEX_ALIASES = {
    "TSE001": "IX0001", "加權": "IX0001", "加權指數": "IX0001", "大盤": "IX0001", "TAIEX": "IX0001",
    "OTC101": "IX0043", "櫃買": "IX0043", "櫃買指數": "IX0043", "TPEX": "IX0043",
}

# 結果中不回傳的欄位：時間戳 (數字對 Gemini 無意義)、帳號與身分資訊
_DROP_FIELDS = {"ts", "account", "account_id", "person_id", "broker_id", "username"}

# (ScannerType 名稱, ascending)。⚠️ shioaji 1.7.8 實測：ascending=True 才是「由大到小」(成交量最大、漲幅最大)，
# 與官方文件相反；升級 shioaji 後請重新實測。
_RANK_TYPES = {
    "漲幅": ("ChangePercentRank", True), "跌幅": ("ChangePercentRank", False),
    "漲跌幅": ("ChangePercentRank", True), "漲跌價": ("ChangePriceRank", True),
    "振幅": ("DayRangeRank", True), "成交量": ("VolumeRank", True), "成交金額": ("AmountRank", True),
}

_session: Dict[str, Any] = {"api": None, "module": None, "production": False}
_lock = threading.Lock()


def _import_shioaji():
    """延遲載入 shioaji (未安裝時其他工具照常可用；測試時以假模組取代)"""
    import shioaji  # noqa: PLC0415
    return shioaji


def _production() -> bool:
    return os.getenv("SJ_PRODUCTION", "false").strip().lower() in ("1", "true", "yes")


def _login_hint(message: str) -> str:
    if "Sign data is timeout" in message:
        return "（電腦時間與永豐伺服器不同步：請在 Windows「設定 → 時間與語言」按「立即同步」後再試）"
    if "401" in message or "Unauthorized" in message.lower():
        return "（金鑰錯誤、過期，或未勾選正式環境權限）"
    return ""


def _get_api():
    """取得已登入的 api (lazy 登入)；失敗時拋出已遮蔽的 RuntimeError"""
    if _session["api"] is not None:
        return _session["api"]
    api_key = os.getenv("SJ_API_KEY", "").strip()
    sec_key = os.getenv("SJ_SEC_KEY", "").strip()
    if not api_key or not sec_key:
        raise RuntimeError("尚未設定永豐金鑰，請在 ArQ 的 .env 加入 SJ_API_KEY 與 SJ_SEC_KEY。")
    try:
        sj = _import_shioaji()
    except ImportError:
        raise RuntimeError("尚未安裝 shioaji 套件，請在 ArQ 目錄執行 `uv add shioaji`。") from None

    production = _production()
    try:
        api = sj.Shioaji(simulation=not production)
        # shioaji 1.7：login 會自動在背景載入合約；不可再呼叫 fetch_contracts()，否則兩者互搶而逾時
        api.login(api_key=api_key, secret_key=sec_key, subscribe_trade=False)
        _wait_contracts(api)
    except Exception as e:
        message = f"{type(e).__name__}: {e}"
        raise RuntimeError(redact(f"永豐登入失敗：{message}{_login_hint(message)}")) from None
    _session.update(api=api, module=sj, production=production)
    return api


def _wait_contracts(api) -> None:
    """等背景合約載入完成 (以 2330 為探針)；逾時不報錯，查報價時會回報查無代碼"""
    deadline = time.monotonic() + CONTRACTS_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            if api.Contracts.Stocks["2330"] is not None:
                return
        except Exception:
            pass
        time.sleep(0.5)


def reset_session() -> None:
    """登出並清除 session (下次查詢重新登入)"""
    api = _session.get("api")
    _session.update(api=None, module=None)
    if api is not None:
        try:
            api.logout()
        except Exception:
            pass


atexit.register(reset_session)


def _run(query) -> Dict[str, Any]:
    """在鎖內登入並執行查詢；查詢失敗時重置 session (連線可能已斷)"""
    with _lock:
        try:
            api = _get_api()
        except RuntimeError as e:
            return {"success": False, "error": str(e)}
        try:
            result = query(api, _session["module"])
        except Exception as e:
            reset_session()
            return {"success": False, "error": redact(f"永豐查詢失敗：{type(e).__name__}: {e}（已重置連線，可再試一次）")}
        env = "正式" if _session["production"] else "模擬"
        return redact_obj({"success": True, "環境": env, **result})


def _plain(value: Any) -> Any:
    if isinstance(value, str):
        return value.strip()
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        return round(value, 4)
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    if hasattr(value, "name") and hasattr(value, "value"):  # Enum
        return str(value.name)
    return str(value)


def _to_dict(obj: Any) -> Dict[str, Any]:
    """把 shioaji 回傳物件轉成精簡 dict (去掉時間戳與帳號資訊)"""
    if isinstance(obj, dict):
        items = obj.items()
    elif callable(getattr(obj, "dict", None)):  # shioaji 1.7 的 PyO3 物件
        items = obj.dict().items()
    elif hasattr(obj, "__dict__") and vars(obj):
        items = vars(obj).items()
    else:
        items = ((k, getattr(obj, k, None)) for k in dir(obj) if not k.startswith("_"))
    return {k: _plain(v) for k, v in items if not k.startswith("_") and k not in _DROP_FIELDS and not callable(v)}


def _sum(rows: List[Dict[str, Any]], field: str) -> float:
    return round(sum(float(r.get(field) or 0) for r in rows), 2)


def shioaji_account_overview(include_odd_lot: bool = False) -> Dict[str, Any]:
    """
    查詢永豐證券帳戶總覽 (唯讀)：證券交割帳戶餘額、證券庫存持倉與未實現損益；有期貨帳戶時一併回傳期貨持倉與保證金。

    Args:
        include_odd_lot: True = 以「股」為單位查詢 (含零股)；False (預設) = 以「張」為單位的整股持倉。
    """
    def query(api, sj):
        unit = sj.Unit.Share if include_odd_lot else sj.Unit.Common
        result: Dict[str, Any] = {"證券餘額": _to_dict(api.account_balance(account=api.stock_account))}
        positions = [_to_dict(p) for p in api.list_positions(account=api.stock_account, unit=unit) or []]
        result["證券持倉單位"] = "股" if include_odd_lot else "張"
        result["證券持倉"] = positions
        result["證券未實現損益合計"] = _sum(positions, "pnl")
        if getattr(api, "futopt_account", None) is not None:
            result["期貨持倉"] = [_to_dict(p) for p in api.list_positions(account=api.futopt_account) or []]
            result["期貨保證金"] = _to_dict(api.margin(account=api.futopt_account))
        return result
    return _run(query)


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def shioaji_profit_loss(begin_date: str = "", end_date: str = "") -> Dict[str, Any]:
    """
    查詢永豐證券帳戶在日期區間內的已實現損益明細與合計 (唯讀)。

    Args:
        begin_date: 開始日期 YYYY-MM-DD；留空 = 今天。
        end_date: 結束日期 YYYY-MM-DD；留空 = 今天。
    """
    today = dt.date.today().isoformat()
    begin, end = (begin_date or today).strip(), (end_date or today).strip()
    if not (_DATE_RE.match(begin) and _DATE_RE.match(end)):
        return {"success": False, "error": "日期格式需為 YYYY-MM-DD，例如 2026-10-01。"}
    try:
        days = (dt.date.fromisoformat(end) - dt.date.fromisoformat(begin)).days
    except ValueError:
        return {"success": False, "error": "日期不存在，請確認年月日。"}
    if days < 0:
        return {"success": False, "error": "開始日期不可晚於結束日期。"}
    if days > MAX_PNL_DAYS:
        return {"success": False, "error": f"查詢區間最多 {MAX_PNL_DAYS} 天。"}

    def query(api, sj):
        rows = [_to_dict(r) for r in api.list_profit_loss(account=api.stock_account, begin_date=begin, end_date=end) or []]
        return {"區間": f"{begin} ~ {end}", "明細": rows, "已實現損益合計": _sum(rows, "pnl")}
    return _run(query)


def _lookup_contract(api, code: str) -> Optional[Any]:
    """依序在股票、指數 (含上市 / 上櫃子表與別名)、期貨合約中尋找代碼"""
    contracts = api.Contracts
    index_code = _INDEX_ALIASES.get(code, code)
    tables = [getattr(contracts, "Stocks", None)]
    indexs = getattr(contracts, "Indexs", None)
    if indexs is not None:
        tables += [indexs, getattr(indexs, "TSE", None), getattr(indexs, "OTC", None)]
    tables.append(getattr(contracts, "Futures", None))
    for table in tables:
        if table is None:
            continue
        key = index_code if table is not tables[0] else code
        try:
            contract = table[key]
        except Exception:
            contract = None
        if contract is not None:
            return contract
    return None


def shioaji_quotes(codes: str) -> Dict[str, Any]:
    """
    查詢台股 / 指數 / 期貨的即時快照 (永豐行情，唯讀)：現價、漲跌、漲跌幅、成交量、買賣價等。

    Args:
        codes: 代碼，多個用逗號分隔。股票如「2330,0050」；指數如「加權」或「IX0001」、「櫃買」或「IX0043」；期貨如「TXFR1」(台指期近月)。
    """
    code_list = [c.strip().upper() for c in (codes or "").split(",") if c.strip()]
    if not code_list:
        return {"success": False, "error": "請提供至少一個代碼，例如「2330」。"}
    if len(code_list) > MAX_QUOTE_CODES:
        return {"success": False, "error": f"一次最多查詢 {MAX_QUOTE_CODES} 個代碼。"}

    def query(api, sj):
        found, missing = [], []
        for code in code_list:
            contract = _lookup_contract(api, code)
            (found if contract is not None else missing).append(contract if contract is not None else code)
        snaps = [_to_dict(s) for s in api.snapshots(found)] if found else []
        names = {getattr(c, "code", ""): getattr(c, "name", "") for c in found}
        for s in snaps:
            if names.get(s.get("code")) and "name" not in s:
                s["name"] = names[s["code"]]
        result: Dict[str, Any] = {"報價": snaps}
        if missing:
            result["查無代碼"] = missing
        return result
    return _run(query)


def shioaji_rankings(rank_type: str = "漲幅", count: int = 10) -> Dict[str, Any]:
    """
    查詢台股當日排行 (永豐行情，唯讀)。

    Args:
        rank_type: 排行種類：漲幅、跌幅、漲跌價、振幅、成交量、成交金額。
        count: 筆數 (1~50，預設 10)。
    """
    spec = _RANK_TYPES.get((rank_type or "").strip())
    if not spec:
        return {"success": False, "error": f"不支援的排行「{rank_type}」，可用：{'、'.join(_RANK_TYPES)}"}
    count = max(1, min(int(count or 10), MAX_RANK_COUNT))
    type_name, ascending = spec

    def query(api, sj):
        rows = api.scanners(scanner_type=getattr(sj.ScannerType, type_name), ascending=ascending, count=count)
        return {"排行": rank_type, "結果": [_to_dict(r) for r in rows or []]}
    return _run(query)


_ACTIONS = ("overview", "profit_loss", "quotes", "rankings")


def shioaji(action: str, codes: str = "", rank_type: str = "漲幅", count: int = 10,
            begin_date: str = "", end_date: str = "", include_odd_lot: bool = False) -> Dict[str, Any]:
    """
    永豐證券唯讀查詢（不能下單）。action：
    overview 帳戶餘額與持倉（include_odd_lot 含零股）；
    profit_loss 已實現損益（begin_date、end_date 為 YYYY-MM-DD，留空=今天）；
    quotes 即時報價（codes 逗號分隔，如「2330,加權,TXFR1」）；
    rankings 當日排行（rank_type 漲幅/跌幅/漲跌價/振幅/成交量/成交金額，count 1~50）。
    """
    name = (action or "").strip().lower()
    if name == "overview":
        return shioaji_account_overview(include_odd_lot=include_odd_lot)
    if name == "profit_loss":
        return shioaji_profit_loss(begin_date=begin_date, end_date=end_date)
    if name == "quotes":
        return shioaji_quotes(codes)
    if name == "rankings":
        return shioaji_rankings(rank_type=rank_type, count=count)
    return {"success": False, "error": f"不支援的 action「{action}」，可用：{'、'.join(_ACTIONS)}。"}
