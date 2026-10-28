"""
台灣天氣工具 (中央氣象署 開放資料平臺 CWA Open Data)

由使用者原本的 weather MCP Server (TypeScript) 移植為 ArQ 原生 Python 工具，不需另外啟動 MCP Server。
    - get_weather_forecast(county, town)：鄉鎮區未來約 3 天預報 (資料集 F-D0047-0xx)
    - get_weather_alerts(county)        ：目前生效中的氣象特報 (資料集 W-C0033-001)
    - weather(action, county, town)     ：給 Gemini 的唯一入口，依 action 分派到上面兩個函式 (節省工具宣告 Token)

金鑰安全 (修改時必須維持)：
    1. 金鑰只放在 .env 的 CWA_API_KEY (已被 .gitignore 忽略)，程式碼 / 文件 / 測試一律不得出現金鑰。
    2. 以 HTTP Header `Authorization` 傳遞金鑰，不放在 URL：urllib 的例外訊息與 log 常會帶出完整 URL。
    3. 所有錯誤訊息回傳前都經過 secret_guard.redact()，就算底層例外帶到金鑰也會被遮蔽。
    4. 回傳給 Gemini 的結果只含天氣資料，不含任何請求細節。

SSL 注意事項 (已實測)：
    CWA 的憑證缺少 Subject Key Identifier 擴充欄位，Python 3.13 預設的嚴格 X.509 檢查
    (VERIFY_X509_STRICT) 會拒絕它。這裡只關閉「嚴格模式」這個旗標，憑證鏈與主機名稱驗證仍照常進行。

Token 節省：完整預報有 3 天、每 3 小時一筆，原樣丟給 Gemini 很耗 Token，
因此只回傳「未來 24 小時每 3 小時的綜合描述」+「每日摘要 (高低溫、最高降雨機率、天氣現象)」。
"""

import json
import os
import ssl
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

from tools.secret_guard import redact

API_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"
ALERTS_DATASET = "W-C0033-001"
REQUEST_TIMEOUT = 20

# 縣市 → 鄉鎮預報資料集 ID (未來 3 天，每 3 小時)
FORECAST_DATASETS: Dict[str, str] = {
    "宜蘭縣": "F-D0047-001", "桃園市": "F-D0047-005", "新竹縣": "F-D0047-009",
    "苗栗縣": "F-D0047-013", "彰化縣": "F-D0047-017", "南投縣": "F-D0047-021",
    "雲林縣": "F-D0047-025", "嘉義縣": "F-D0047-029", "屏東縣": "F-D0047-033",
    "臺東縣": "F-D0047-037", "花蓮縣": "F-D0047-041", "澎湖縣": "F-D0047-045",
    "基隆市": "F-D0047-049", "新竹市": "F-D0047-053", "嘉義市": "F-D0047-057",
    "臺北市": "F-D0047-061", "高雄市": "F-D0047-065", "新北市": "F-D0047-069",
    "臺中市": "F-D0047-073", "臺南市": "F-D0047-077", "連江縣": "F-D0047-081",
    "金門縣": "F-D0047-085",
}

# 未來幾小時內逐 3 小時提供詳細描述
DETAIL_HOURS = 24

_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.verify_flags &= ~ssl.VERIFY_X509_STRICT


def _api_key() -> str:
    return os.getenv("CWA_API_KEY", "").strip()


def normalize_county(county: str) -> Optional[str]:
    """把使用者輸入的縣市名稱轉成資料集使用的正式名稱 (台→臺，可省略「縣/市」)；找不到回傳 None"""
    name = (county or "").strip().replace("台", "臺")
    if not name:
        return None
    if name in FORECAST_DATASETS:
        return name
    for full in FORECAST_DATASETS:  # 「臺北」→「臺北市」
        if full[:-1] == name:
            return full
    return None


def _request(dataset: str, params: Dict[str, str]) -> Dict[str, Any]:
    """呼叫 CWA API；金鑰走 Header。失敗時拋出已遮蔽金鑰的 RuntimeError。"""
    key = _api_key()
    if not key:
        raise RuntimeError("尚未設定 CWA_API_KEY，請在 .env 加入中央氣象署開放資料平臺的授權碼。")

    url = API_BASE + dataset
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": key, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT, context=_SSL_CONTEXT) as resp:
            data = json.load(resp)
    except urllib.error.HTTPError as e:
        hint = " (授權碼可能無效或已過期)" if e.code in (401, 403) else ""
        raise RuntimeError(redact(f"氣象署 API 回應錯誤 HTTP {e.code}{hint}")) from None
    except Exception as e:
        raise RuntimeError(redact(f"連線氣象署 API 失敗：{type(e).__name__}: {e}")) from None

    if str(data.get("success")).lower() != "true":
        raise RuntimeError("氣象署 API 回傳失敗結果，請稍後再試。")
    return data


def _short_time(ts: str) -> str:
    """'2026-10-15T12:00:00+08:00' → '10/15 12:00'"""
    ts = (ts or "").replace("T", " ")
    return f"{ts[5:7]}/{ts[8:10]} {ts[11:16]}" if len(ts) >= 16 else ts


def _element(location: Dict[str, Any], name: str) -> List[Dict[str, Any]]:
    for el in location.get("WeatherElement") or []:
        if el.get("ElementName") == name:
            return el.get("Time") or []
    return []


def _value(entry: Dict[str, Any], field: str) -> Optional[str]:
    values = entry.get("ElementValue") or [{}]
    return values[0].get(field)


def _to_int(text: Optional[str]) -> Optional[int]:
    try:
        return int(text)
    except (TypeError, ValueError):
        return None


def _summarize_forecast(location: Dict[str, Any]) -> Dict[str, Any]:
    """把完整預報濃縮成「24 小時逐 3 小時描述 + 每日摘要」"""
    descriptions = _element(location, "天氣預報綜合描述")
    detail = [
        {"時段": f"{_short_time(t.get('StartTime'))}~{_short_time(t.get('EndTime'))[-5:]}",
         "描述": _value(t, "WeatherDescription")}
        for t in descriptions[: DETAIL_HOURS // 3]
    ]

    days: Dict[str, Dict[str, Any]] = {}

    def day_of(entry: Dict[str, Any]) -> str:
        return _short_time(entry.get("DataTime") or entry.get("StartTime"))[:5]

    for t in _element(location, "溫度"):
        temp = _to_int(_value(t, "Temperature"))
        if temp is None:
            continue
        d = days.setdefault(day_of(t), {"temps": [], "pops": [], "weather": []})
        d["temps"].append(temp)
    for t in _element(location, "3小時降雨機率"):
        pop = _to_int(_value(t, "ProbabilityOfPrecipitation"))
        if pop is not None:
            days.setdefault(day_of(t), {"temps": [], "pops": [], "weather": []})["pops"].append(pop)
    for t in _element(location, "天氣現象"):
        weather = _value(t, "Weather")
        d = days.setdefault(day_of(t), {"temps": [], "pops": [], "weather": []})
        if weather and weather not in d["weather"]:
            d["weather"].append(weather)

    daily = []
    for date, d in days.items():
        item: Dict[str, Any] = {"日期": date}
        if d["temps"]:
            item["氣溫"] = f"{min(d['temps'])}~{max(d['temps'])}°C"
        if d["pops"]:
            item["最高降雨機率"] = f"{max(d['pops'])}%"
        if d["weather"]:
            item["天氣"] = "、".join(d["weather"][:4])
        daily.append(item)

    return {"未來24小時": detail, "每日摘要": daily}


def get_weather_forecast(county: str, town: str) -> Dict[str, Any]:
    """
    查詢台灣指定鄉鎮區的天氣預報 (中央氣象署官方資料，未來約 3 天)。
    回傳未來 24 小時每 3 小時的天氣描述，以及每日的高低溫、最高降雨機率與天氣現象。
    查台灣天氣時請優先使用本工具，比上網搜尋更準確也更省資源。

    Args:
        county: 縣市名稱 (中文)，例如「臺北市」、「台南市」、「高雄」。
        town: 鄉鎮區名稱 (中文)，例如「內湖區」、「佳里區」。
    """
    # 註：上方 docstring 會被 Gemini 當成工具說明。
    full_county = normalize_county(county)
    if not full_county:
        return {"success": False, "error": f"不支援的縣市「{county}」。可用：{'、'.join(FORECAST_DATASETS)}"}
    town = (town or "").strip().replace("台", "臺")
    if not town:
        return {"success": False, "error": "請提供鄉鎮區名稱，例如「內湖區」。"}

    try:
        data = _request(FORECAST_DATASETS[full_county], {"LocationName": town})
    except RuntimeError as e:
        return {"success": False, "error": str(e)}

    locations = ((data.get("records") or {}).get("Locations") or [{}])[0].get("Location") or []
    if not locations:
        return {"success": False, "error": f"找不到「{full_county}{town}」，請確認鄉鎮區名稱 (例如「內湖區」需含「區」)。"}

    return {"success": True, "地點": f"{full_county}{locations[0].get('LocationName', town)}",
            "資料來源": "中央氣象署", **_summarize_forecast(locations[0])}


def get_weather_alerts(county: str = "") -> Dict[str, Any]:
    """
    查詢目前生效中的氣象災害特報 (中央氣象署官方資料)，例如豪雨、大雨、強風、颱風、低溫特報。

    Args:
        county: 縣市名稱 (中文)，例如「臺北市」；留空則查詢全台灣。
    """
    target = None
    if county and county.strip():
        target = normalize_county(county)
        if not target:
            return {"success": False, "error": f"不支援的縣市「{county}」。可用：{'、'.join(FORECAST_DATASETS)}"}

    try:
        data = _request(ALERTS_DATASET, {})
    except RuntimeError as e:
        return {"success": False, "error": str(e)}

    alerts = []
    for loc in (data.get("records") or {}).get("location") or []:
        name = loc.get("locationName", "")
        if target and name != target:
            continue
        for hazard in (loc.get("hazardConditions") or {}).get("hazards") or []:
            info = hazard.get("info") or {}
            valid = hazard.get("validTime") or {}
            alerts.append({
                "縣市": name,
                "特報": f"{info.get('phenomena', '')}{info.get('significance', '')}",
                "生效": (valid.get("startTime") or "")[5:16],
                "結束": (valid.get("endTime") or "")[5:16],
            })

    scope = target or "全台灣"
    if not alerts:
        return {"success": True, "範圍": scope, "特報": [], "說明": f"{scope}目前沒有生效中的氣象特報。"}
    return {"success": True, "範圍": scope, "特報": alerts, "資料來源": "中央氣象署"}


def weather(action: str, county: str = "", town: str = "") -> Dict[str, Any]:
    """
    台灣天氣（中央氣象署）。action：forecast 鄉鎮預報（county 縣市＋town 鄉鎮區，皆需提供）；
    alerts 氣象特報（county 可留空查全台）。
    """
    name = (action or "").strip().lower()
    if name == "forecast":
        return get_weather_forecast(county, town)
    if name == "alerts":
        return get_weather_alerts(county)
    return {"success": False, "error": f"不支援的 action「{action}」，可用：forecast、alerts。"}
