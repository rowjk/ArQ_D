"""
tools/shioaji_tools.py 單元測試 (不連網、不需安裝 shioaji：以假模組 / 假 api 物件取代)

重點：
    - 金鑰只從 .env 的 SJ_API_KEY / SJ_SEC_KEY 讀取，缺少時不登入；錯誤訊息遮蔽金鑰
    - 登入不訂閱委託回報 (唯讀金鑰沒有交易權限)、預設模擬環境，SJ_PRODUCTION=true 才連正式
    - 只做唯讀查詢：模組原始碼不得出現下單 / 改單 / 刪單 / 啟用 CA
    - 連線失敗時重置 session，下次重新登入
    - 帳務、損益、報價、排行的結果轉為精簡 dict
"""

import os
import re
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import shioaji_tools as st  # noqa: E402

FAKE_API_KEY = "FAKEapiKEY0000111122223333444455556666"
FAKE_SEC_KEY = "FAKEsecKEY0000111122223333444455556666"


class FakeEnum:
    def __init__(self, name):
        self.name = name
        self.value = name

    def __str__(self):
        return f"Enum.{self.name}"


class FakeContracts:
    def __init__(self):
        self.Stocks = {"2330": SimpleNamespace(code="2330", name="台積電"),
                       "0050": SimpleNamespace(code="0050", name="元大台灣50")}
        self.Indexs = SimpleNamespace(TSE={"IX0001": SimpleNamespace(code="IX0001", name="發行量加權股價指數")},
                                      OTC={"IX0043": SimpleNamespace(code="IX0043", name="櫃買指數")})
        self.Futures = {"TXFR1": SimpleNamespace(code="TXFR1", name="台指期近月")}


class FakeApi:
    def __init__(self, futopt=True):
        self.Contracts = FakeContracts()
        self.stock_account = SimpleNamespace(account_type="S")
        self.futopt_account = SimpleNamespace(account_type="F") if futopt else None
        self.calls = []

    def account_balance(self, account=None):
        self.calls.append("account_balance")
        return SimpleNamespace(acc_balance=123456.0, date="2026-10-16", errmsg="")

    def list_positions(self, account=None, unit=None):
        self.calls.append(("list_positions", account.account_type, getattr(unit, "name", unit)))
        if account.account_type == "S":
            return [SimpleNamespace(id=0, code="2330", direction=FakeEnum("Buy"), quantity=2,
                                    price=900.0, last_price=1000.0, pnl=200000.0)]
        return [SimpleNamespace(id=0, code="TXFR1", direction=FakeEnum("Sell"), quantity=1,
                                price=20000.0, last_price=19900.0, pnl=20000.0)]

    def margin(self, account=None):
        self.calls.append("margin")
        return SimpleNamespace(equity=500000.0, available_margin=300000.0, risk_indicator=250.0)

    def list_profit_loss(self, account=None, begin_date="", end_date=""):
        self.calls.append(("list_profit_loss", begin_date, end_date))
        return [SimpleNamespace(id=0, code="2330", quantity=1000, pnl=5000.0, date="2026-10-01"),
                SimpleNamespace(id=1, code="0050", quantity=500, pnl=-1200.0, date="2026-10-02")]

    def snapshots(self, contracts):
        self.calls.append(("snapshots", [c.code for c in contracts]))
        return [SimpleNamespace(code=c.code, close=1000.0, change_price=10.0, change_rate=1.01,
                                total_volume=30000, ts=1760600000000000000) for c in contracts]

    def scanners(self, scanner_type=None, ascending=False, count=10):
        self.calls.append(("scanners", scanner_type, ascending, count))
        return [SimpleNamespace(code="2330", name="台積電", close=1000.0, change_price=50.0,
                                total_volume=40000, ts=1)]


class FakeShioajiModule:
    """取代 import shioaji：記錄建構與登入參數"""

    def __init__(self, login_error=None):
        self.created = []
        self.login_kwargs = None
        self.login_error = login_error
        self.Unit = SimpleNamespace(Common=FakeEnum("Common"), Share=FakeEnum("Share"))
        self.ScannerType = SimpleNamespace(**{n: FakeEnum(n) for n in (
            "ChangePercentRank", "ChangePriceRank", "DayRangeRank", "VolumeRank", "AmountRank")})
        module = self

        class Shioaji(FakeApi):
            def __init__(self, simulation=True):
                super().__init__()
                module.created.append({"simulation": simulation})
                self.logged_out = False

            def login(self, **kwargs):
                module.login_kwargs = kwargs
                if module.login_error:
                    raise module.login_error
                return [self.stock_account]

            def fetch_contracts(self, *args, **kwargs):
                module.fetch_called = True  # 1.7 login 已自動載入，再呼叫會互搶逾時

            def logout(self):
                self.logged_out = True

        self.Shioaji = Shioaji


def _env(**extra):
    env = {"SJ_API_KEY": FAKE_API_KEY, "SJ_SEC_KEY": FAKE_SEC_KEY, "SJ_PRODUCTION": "false"}
    env.update(extra)
    return patch.dict(os.environ, env)


class SessionTestBase(unittest.TestCase):
    def setUp(self):
        st.reset_session()
        self.module = FakeShioajiModule()
        self._p = patch.object(st, "_import_shioaji", lambda: self.module)
        self._p.start()

    def tearDown(self):
        self._p.stop()
        st.reset_session()


class TestLogin(SessionTestBase):
    def test_missing_keys_does_not_login(self):
        with patch.dict(os.environ, {"SJ_API_KEY": "", "SJ_SEC_KEY": ""}):
            res = st.shioaji_account_overview()
        self.assertFalse(res["success"])
        self.assertIn("SJ_API_KEY", res["error"])
        self.assertEqual(self.module.created, [])

    def test_login_readonly_and_simulation_by_default(self):
        with _env():
            res = st.shioaji_account_overview()
        self.assertTrue(res["success"])
        self.assertEqual(res["環境"], "模擬")
        self.assertEqual(self.module.created, [{"simulation": True}])
        kw = self.module.login_kwargs
        self.assertEqual(kw["api_key"], FAKE_API_KEY)
        self.assertEqual(kw["secret_key"], FAKE_SEC_KEY)
        self.assertFalse(kw["subscribe_trade"])
        self.assertEqual(set(kw), {"api_key", "secret_key", "subscribe_trade"})  # 1.7 的 login 參數
        self.assertFalse(getattr(self.module, "fetch_called", False))

    def test_production_flag(self):
        with _env(SJ_PRODUCTION="true"):
            res = st.shioaji_account_overview()
        self.assertEqual(res["環境"], "正式")
        self.assertEqual(self.module.created, [{"simulation": False}])

    def test_session_reused(self):
        with _env():
            st.shioaji_account_overview()
            st.shioaji_quotes("2330")
        self.assertEqual(len(self.module.created), 1)

    def test_login_error_redacted_with_clock_hint(self):
        self.module.login_error = RuntimeError(f"Sign data is timeout key={FAKE_API_KEY}")
        with _env():
            res = st.shioaji_account_overview()
        self.assertFalse(res["success"])
        self.assertNotIn(FAKE_API_KEY, res["error"])
        self.assertIn("時間", res["error"])

    def test_query_error_resets_session(self):
        with _env():
            st.shioaji_account_overview()
            api = st._session["api"]
            with patch.object(api, "account_balance", side_effect=RuntimeError("session not established")):
                res = st.shioaji_account_overview()
            self.assertFalse(res["success"])
            self.assertIsNone(st._session["api"])
            st.shioaji_account_overview()
        self.assertEqual(len(self.module.created), 2)

    def test_reset_session_logs_out(self):
        with _env():
            st.shioaji_account_overview()
        api = st._session["api"]
        st.reset_session()
        self.assertTrue(api.logged_out)


class TestQueries(SessionTestBase):
    def test_account_overview(self):
        with _env():
            res = st.shioaji_account_overview()
        self.assertEqual(res["證券餘額"]["acc_balance"], 123456.0)
        self.assertEqual(res["證券持倉"][0]["code"], "2330")
        self.assertEqual(res["證券持倉"][0]["direction"], "Buy")
        self.assertEqual(res["證券未實現損益合計"], 200000.0)
        self.assertEqual(res["期貨持倉"][0]["code"], "TXFR1")
        self.assertEqual(res["期貨保證金"]["equity"], 500000.0)

    def test_account_overview_without_futures_account(self):
        self.module.Shioaji.__init__  # noqa: B018 (確保類別存在)
        with _env():
            st.shioaji_account_overview()
            st._session["api"].futopt_account = None
            res = st.shioaji_account_overview()
        self.assertTrue(res["success"])
        self.assertNotIn("期貨持倉", res)

    def test_odd_lot_uses_share_unit(self):
        with _env():
            st.shioaji_account_overview(include_odd_lot=True)
        calls = st._session["api"].calls
        self.assertIn(("list_positions", "S", "Share"), calls)

    def test_profit_loss_with_total(self):
        with _env():
            res = st.shioaji_profit_loss("2026-10-01", "2026-10-16")
        self.assertTrue(res["success"])
        self.assertEqual(len(res["明細"]), 2)
        self.assertEqual(res["已實現損益合計"], 3800.0)
        self.assertIn(("list_profit_loss", "2026-10-01", "2026-10-16"), st._session["api"].calls)

    def test_profit_loss_rejects_bad_dates(self):
        with _env():
            for args in (("2026/10/01", "2026-10-16"), ("2026-10-16", "2026-10-01")):
                with self.subTest(args=args):
                    self.assertFalse(st.shioaji_profit_loss(*args)["success"])
        self.assertEqual(self.module.created, [])

    def test_quotes_lookup_and_not_found(self):
        with _env():
            res = st.shioaji_quotes("2330, 加權,TSE001,otc101,TXFR1, 9999")
        self.assertTrue(res["success"])
        self.assertEqual([q["code"] for q in res["報價"]], ["2330", "IX0001", "IX0001", "IX0043", "TXFR1"])
        self.assertEqual(res["查無代碼"], ["9999"])
        self.assertNotIn("ts", res["報價"][0])

    def test_quotes_limit(self):
        codes = ",".join(str(1000 + i) for i in range(st.MAX_QUOTE_CODES + 1))
        with _env():
            res = st.shioaji_quotes(codes)
        self.assertFalse(res["success"])

    def test_rankings(self):
        with _env():
            res = st.shioaji_rankings("成交量", count=5)
        self.assertTrue(res["success"])
        _, scanner_type, ascending, count = st._session["api"].calls[-1]
        self.assertEqual(scanner_type.name, "VolumeRank")
        self.assertTrue(ascending)  # shioaji 1.7.8 實測：True = 由大到小
        self.assertEqual(count, 5)

    def test_rankings_unknown_type(self):
        with _env():
            res = st.shioaji_rankings("人氣")
        self.assertFalse(res["success"])


class TestSerialization(unittest.TestCase):
    def test_pyo3_dict_method_and_strip(self):
        class PyO3Like:
            def dict(self):
                return {"code": "1413", "name": "宏洲      ", "ts": 1, "pnl": 1.23456}
        self.assertEqual(st._to_dict(PyO3Like()), {"code": "1413", "name": "宏洲", "pnl": 1.2346})


class TestSafety(unittest.TestCase):
    def test_source_has_no_trading_calls(self):
        with open(st.__file__, encoding="utf-8") as f:
            code = f.read()
        for word in ("place_order", "cancel_order", "update_order", "update_status",
                     "activate_ca", "place_comboorder"):
            self.assertIsNone(re.search(rf"\.{word}\s*\(", code), word)

    def test_sj_keys_are_treated_as_secrets(self):
        from tools import secret_guard
        with patch.dict(os.environ, {"SJ_API_KEY": FAKE_API_KEY, "SJ_SEC_KEY": FAKE_SEC_KEY}):
            self.assertEqual(secret_guard.redact(f"a {FAKE_API_KEY} b {FAKE_SEC_KEY}"), "a *** b ***")
            env = secret_guard.child_env()
        self.assertNotIn("SJ_API_KEY", env)
        self.assertNotIn("SJ_SEC_KEY", env)

    def test_registered_in_agent_as_readonly(self):
        import agent
        self.assertIn("shioaji", agent.TOOL_REGISTRY)
        self.assertIsNone(agent.TOOL_REGISTRY["shioaji"].risk)


if __name__ == "__main__":
    unittest.main()
