"""
tools/weather_tools.py 單元測試 (不連網，以假資料 mock API 回應)

重點：
    - 金鑰只走 HTTP Header，不出現在 URL
    - 錯誤訊息不得帶出金鑰
    - 縣市名稱正規化 (台→臺、省略縣/市)
    - 預報濃縮為「24 小時逐 3 小時描述 + 每日摘要」；特報依縣市過濾
"""

import io
import json
import os
import sys
import unittest
import urllib.error
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import weather_tools as wt  # noqa: E402

FAKE_KEY = "CWA-FAKE0000-1111-2222-3333-444455556666"


def _forecast_payload():
    def times(n, start_hour, value_fn, ranged):
        out = []
        for i in range(n):
            h = start_hour + 3 * i
            day, hour = 15 + h // 24, h % 24
            ts = f"2026-10-{day:02d}T{hour:02d}:00:00+08:00"
            entry = {"ElementValue": [value_fn(i)]}
            if ranged:
                h2 = h + 3
                entry["StartTime"] = ts
                entry["EndTime"] = f"2026-10-{15 + h2 // 24:02d}T{h2 % 24:02d}:00:00+08:00"
            else:
                entry["DataTime"] = ts
            out.append(entry)
        return out

    return {
        "success": "true",
        "records": {"Locations": [{"LocationsName": "臺北市", "Location": [{
            "LocationName": "內湖區",
            "WeatherElement": [
                {"ElementName": "溫度", "Time": times(16, 12, lambda i: {"Temperature": str(22 + i % 6)}, False)},
                {"ElementName": "3小時降雨機率", "Time": times(16, 12, lambda i: {"ProbabilityOfPrecipitation": str(10 * (i % 4))}, True)},
                {"ElementName": "天氣現象", "Time": times(16, 12, lambda i: {"Weather": "陰" if i % 2 else "多雲"}, True)},
                {"ElementName": "天氣預報綜合描述", "Time": times(16, 12, lambda i: {"WeatherDescription": f"描述{i}"}, True)},
            ],
        }]}]},
    }


ALERTS_PAYLOAD = {
    "success": "true",
    "records": {"location": [
        {"locationName": "屏東縣", "hazardConditions": {"hazards": [
            {"info": {"phenomena": "陸上強風", "significance": "特報"},
             "validTime": {"startTime": "2026-10-15 10:23:00", "endTime": "2026-10-15 22:00:00"}}]}},
        {"locationName": "臺北市", "hazardConditions": {"hazards": []}},
    ]},
}


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class TestNormalize(unittest.TestCase):
    def test_county_names(self):
        self.assertEqual(wt.normalize_county("台北市"), "臺北市")
        self.assertEqual(wt.normalize_county("臺南"), "臺南市")
        self.assertEqual(wt.normalize_county("高雄"), "高雄市")
        self.assertEqual(wt.normalize_county("宜蘭"), "宜蘭縣")
        self.assertIsNone(wt.normalize_county("東京都"))
        self.assertIsNone(wt.normalize_county(""))


@patch.dict(os.environ, {"CWA_API_KEY": FAKE_KEY})
class TestRequests(unittest.TestCase):
    def test_key_sent_in_header_not_url(self):
        captured = {}

        def fake_urlopen(req, timeout, context):
            captured["url"] = req.full_url
            captured["auth"] = req.get_header("Authorization")
            return FakeResponse(json.dumps(_forecast_payload()).encode("utf-8"))

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            res = wt.get_weather_forecast("台北市", "內湖區")
        self.assertTrue(res["success"])
        self.assertNotIn(FAKE_KEY, captured["url"])
        self.assertEqual(captured["auth"], FAKE_KEY)
        self.assertIn("F-D0047-061", captured["url"])
        self.assertNotIn(FAKE_KEY, json.dumps(res, ensure_ascii=False))

    def test_error_message_never_contains_key(self):
        def boom(req, timeout, context):
            raise urllib.error.URLError(f"failed for https://x/?Authorization={FAKE_KEY}")

        with patch("urllib.request.urlopen", side_effect=boom):
            res = wt.get_weather_alerts()
        self.assertFalse(res["success"])
        self.assertNotIn(FAKE_KEY, res["error"])

    def test_http_401_hint(self):
        def unauthorized(req, timeout, context):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        with patch("urllib.request.urlopen", side_effect=unauthorized):
            res = wt.get_weather_forecast("臺北市", "內湖區")
        self.assertIn("授權碼", res["error"])

    def test_missing_key(self):
        with patch.dict(os.environ, {"CWA_API_KEY": ""}):
            res = wt.get_weather_alerts()
        self.assertFalse(res["success"])
        self.assertIn("CWA_API_KEY", res["error"])


@patch.dict(os.environ, {"CWA_API_KEY": FAKE_KEY})
class TestParsing(unittest.TestCase):
    def test_forecast_summary(self):
        with patch.object(wt, "_request", return_value=_forecast_payload()):
            res = wt.get_weather_forecast("臺北市", "內湖區")
        self.assertEqual(res["地點"], "臺北市內湖區")
        self.assertEqual(len(res["未來24小時"]), 8)  # 24 小時 / 每 3 小時
        self.assertEqual(res["未來24小時"][0]["描述"], "描述0")
        self.assertEqual(res["未來24小時"][0]["時段"], "10/15 12:00~15:00")
        first_day = res["每日摘要"][0]
        self.assertEqual(first_day["日期"], "10/15")
        self.assertIn("°C", first_day["氣溫"])
        self.assertIn("%", first_day["最高降雨機率"])
        self.assertIn("多雲", first_day["天氣"])

    def test_town_not_found(self):
        empty = {"success": "true", "records": {"Locations": [{"Location": []}]}}
        with patch.object(wt, "_request", return_value=empty):
            res = wt.get_weather_forecast("臺北市", "不存在區")
        self.assertFalse(res["success"])
        self.assertIn("找不到", res["error"])

    def test_unknown_county(self):
        res = wt.get_weather_forecast("東京都", "新宿區")
        self.assertFalse(res["success"])

    def test_alerts_filter(self):
        with patch.object(wt, "_request", return_value=ALERTS_PAYLOAD):
            all_res = wt.get_weather_alerts()
            pt = wt.get_weather_alerts("屏東")
            tp = wt.get_weather_alerts("台北市")
        self.assertEqual(len(all_res["特報"]), 1)
        self.assertEqual(pt["特報"][0]["特報"], "陸上強風特報")
        self.assertEqual(pt["特報"][0]["生效"], "10-15 10:23")
        self.assertEqual(tp["特報"], [])
        self.assertIn("沒有生效中的氣象特報", tp["說明"])

    def test_registered_in_agent(self):
        import agent
        self.assertIn("weather", agent.TOOL_REGISTRY)
        self.assertIsNone(agent.TOOL_REGISTRY["weather"].risk)  # 唯讀，直接執行


if __name__ == "__main__":
    unittest.main()
