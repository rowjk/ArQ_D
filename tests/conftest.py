"""
pytest 設定：測試全面禁止連外

unittest 的 TestCase 由 netguard 包住 TestCase.run 判定；
這裡的 autouse fixture 處理一般 pytest 測試函式 (例外被吞掉時仍判定失敗)。
"""

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import pytest  # noqa: E402

from tests import netguard  # noqa: E402  測試禁止連外


@pytest.fixture(autouse=True)
def _block_network(request):
    test_id = request.node.nodeid
    yield
    violations = netguard.pop_violations(test_id) + netguard.pop_violations("未知測試")
    if violations:
        pytest.fail("\n".join(violations), pytrace=False)
