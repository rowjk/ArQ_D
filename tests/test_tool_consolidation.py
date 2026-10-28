"""
工具宣告瘦身：永豐 4 合 1、氣象 2 合 1、認證檢查不暴露給 Gemini

重點：
    - shioaji(action, ...) / weather(action, ...) 依 action 轉給既有查詢函式，未知 action 回報錯誤
    - 合併後仍為唯讀 (risk=None)，且不得有任何下單 / 改單 / 刪單入口
    - check_claude_auth / check_antigravity_auth 不在工具清單 (派工流程仍可呼叫)
    - 工具宣告字元數有上限，系統提示詞只提到存在的工具
"""

import json
import os
import re
import sys
import unittest
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from google.genai import types  # noqa: E402

import agent  # noqa: E402
from tools import rd_dispatch, shioaji_tools, weather_tools  # noqa: E402

OLD_SHIOAJI = ("shioaji_account_overview", "shioaji_profit_loss", "shioaji_quotes", "shioaji_rankings")
OLD_WEATHER = ("get_weather_forecast", "get_weather_alerts")
AUTH_TOOLS = ("check_claude_auth", "check_antigravity_auth")

# 重構前實測 (19 個工具)：宣告共 7,514 字元，其中永豐 1,253、氣象 647、認證檢查 184
BASELINE_TOTAL_CHARS = 7514
MAX_TOTAL_DECLARATION_CHARS = 6500
MAX_SHIOAJI_CHARS = 700
MAX_WEATHER_CHARS = 450


def declaration_chars(func) -> int:
    decl = types.FunctionDeclaration.from_callable_with_api_option(callable=func, api_option="GEMINI_API")
    return len(json.dumps(decl.model_dump(mode="json", exclude_none=True), ensure_ascii=False))


class TestShioajiDispatcher(unittest.TestCase):
    def test_routes_each_action(self):
        cases = [
            ({"action": "overview", "include_odd_lot": True}, "shioaji_account_overview", ((), {"include_odd_lot": True})),
            ({"action": "profit_loss", "begin_date": "2026-10-01", "end_date": "2026-10-16"}, "shioaji_profit_loss",
             ((), {"begin_date": "2026-10-01", "end_date": "2026-10-16"})),
            ({"action": "quotes", "codes": "2330,加權"}, "shioaji_quotes", (("2330,加權",), {})),
            ({"action": "rankings", "rank_type": "成交量", "count": 5}, "shioaji_rankings", ((), {"rank_type": "成交量", "count": 5})),
        ]
        for kwargs, target, (exp_args, exp_kwargs) in cases:
            with self.subTest(action=kwargs["action"]), \
                 patch.object(shioaji_tools, target, return_value={"success": True}) as mocked:
                self.assertEqual(shioaji_tools.shioaji(**kwargs), {"success": True})
                mocked.assert_called_once_with(*exp_args, **exp_kwargs)

    def test_action_is_case_and_space_insensitive(self):
        with patch.object(shioaji_tools, "shioaji_quotes", return_value={"success": True}) as mocked:
            shioaji_tools.shioaji(action=" Quotes ", codes="2330")
        mocked.assert_called_once()

    def test_unknown_action_is_rejected_without_login(self):
        with patch.object(shioaji_tools, "_get_api") as login:
            for action in ("place_order", "", "cancel"):
                with self.subTest(action=action):
                    res = shioaji_tools.shioaji(action=action)
                    self.assertFalse(res["success"])
                    self.assertIn("overview", res["error"])
        login.assert_not_called()

    def test_quotes_without_codes_reports_error(self):
        res = shioaji_tools.shioaji(action="quotes")
        self.assertFalse(res["success"])

    def test_real_end_to_end_validation_still_applies(self):
        res = shioaji_tools.shioaji(action="profit_loss", begin_date="2026/10/01")
        self.assertFalse(res["success"])
        res = shioaji_tools.shioaji(action="rankings", rank_type="人氣")
        self.assertFalse(res["success"])


class TestWeatherDispatcher(unittest.TestCase):
    def test_routes_actions(self):
        with patch.object(weather_tools, "get_weather_forecast", return_value={"success": True}) as fc:
            weather_tools.weather(action="forecast", county="臺北市", town="內湖區")
        fc.assert_called_once_with("臺北市", "內湖區")
        with patch.object(weather_tools, "get_weather_alerts", return_value={"success": True}) as al:
            weather_tools.weather(action="alerts", county="屏東")
            weather_tools.weather(action="Alerts")
        self.assertEqual([c.args for c in al.call_args_list], [("屏東",), ("",)])

    def test_unknown_action(self):
        res = weather_tools.weather(action="radar")
        self.assertFalse(res["success"])
        self.assertIn("forecast", res["error"])

    def test_forecast_requires_town_via_underlying_validation(self):
        res = weather_tools.weather(action="forecast", county="臺北市")
        self.assertFalse(res["success"])
        self.assertIn("鄉鎮區", res["error"])


class TestRegistry(unittest.TestCase):
    def test_merged_tools_registered_readonly(self):
        for name in ("shioaji", "weather"):
            with self.subTest(name=name):
                self.assertIn(name, agent.TOOL_REGISTRY)
                self.assertIsNone(agent.TOOL_REGISTRY[name].risk)
                self.assertIsNone(agent.TOOL_REGISTRY[name].impl)

    def test_old_tools_not_exposed(self):
        for name in OLD_SHIOAJI + OLD_WEATHER + AUTH_TOOLS:
            with self.subTest(name=name):
                self.assertNotIn(name, agent.TOOL_REGISTRY)
        exposed = {spec.func.__name__ for spec in agent._TOOL_SPECS}
        self.assertFalse(exposed & set(OLD_SHIOAJI + OLD_WEATHER + AUTH_TOOLS))

    def test_auth_checks_still_used_by_dispatch_flow(self):
        for name in AUTH_TOOLS:
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(rd_dispatch, name)))

    def test_shioaji_tool_has_no_trading_surface(self):
        params = set(agent.TOOL_REGISTRY["shioaji"].func.__code__.co_varnames[: agent.TOOL_REGISTRY["shioaji"].func.__code__.co_argcount])
        self.assertTrue(params.isdisjoint({"order", "price", "quantity", "cancel", "ca", "account"}))
        with open(shioaji_tools.__file__, encoding="utf-8") as f:
            code = f.read()
        for word in ("place_order", "cancel_order", "update_order", "update_status", "activate_ca", "place_comboorder"):
            self.assertIsNone(re.search(rf"\.{word}\s*\(", code), word)


class TestDeclarationBudget(unittest.TestCase):
    def test_merged_declarations_are_small(self):
        self.assertLessEqual(declaration_chars(shioaji_tools.shioaji), MAX_SHIOAJI_CHARS)
        self.assertLessEqual(declaration_chars(weather_tools.weather), MAX_WEATHER_CHARS)

    def test_total_declarations_shrink(self):
        total = sum(declaration_chars(spec.func) for spec in agent._TOOL_SPECS)
        self.assertLessEqual(total, MAX_TOTAL_DECLARATION_CHARS)
        self.assertLess(total, BASELINE_TOTAL_CHARS)

    def test_tool_count(self):
        self.assertEqual(len(agent._TOOL_SPECS), 13)  # 原 19 個：永豐 4→1、氣象 2→1、移除 2 個認證檢查


class TestSystemInstruction(unittest.TestCase):
    def test_mentions_merged_tools_with_actions(self):
        text = agent.SYSTEM_INSTRUCTION
        self.assertIn("weather", text)
        self.assertIn("shioaji", text)
        for action in ("forecast", "alerts", "overview", "profit_loss", "quotes", "rankings"):
            with self.subTest(action=action):
                self.assertIn(action, text)

    def test_does_not_mention_removed_tools(self):
        for name in OLD_SHIOAJI + OLD_WEATHER + AUTH_TOOLS:
            with self.subTest(name=name):
                self.assertNotIn(name, agent.SYSTEM_INSTRUCTION)
        self.assertNotIn("shioaji_ 工具", agent.SYSTEM_INSTRUCTION)

    def test_http_tool_doc_points_to_new_tool(self):
        from tools import http_tools
        self.assertNotIn("shioaji_ 開頭", http_tools.call_api.__doc__)
        self.assertIn("shioaji", http_tools.call_api.__doc__)


if __name__ == "__main__":
    unittest.main()
