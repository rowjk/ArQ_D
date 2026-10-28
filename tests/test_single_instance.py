"""tools/single_instance.py 單元測試：同一時間只能有一個 ArQ bot"""

import os
import subprocess
import sys
import unittest
import uuid

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

from tools import single_instance  # noqa: E402


@unittest.skipUnless(sys.platform == "win32", "Windows 具名 Mutex")
class TestSingleInstance(unittest.TestCase):
    def test_second_process_is_rejected(self):
        name = f"Local\\ArQ-test-{uuid.uuid4().hex}"  # 測試用名稱，不影響真正在跑的 ArQ
        self.assertTrue(single_instance.acquire(name))
        probe = (
            "import sys; sys.path.insert(0, r'%s'); from tools import single_instance; "
            "print(single_instance.acquire(r'%s'))" % (PROJECT_ROOT, name)
        )
        out = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "False", "已有實例在跑時，第二個程序必須被拒絕")

    def test_released_after_process_exit(self):
        name = f"Local\\ArQ-test-{uuid.uuid4().hex}"
        probe = (
            "import sys; sys.path.insert(0, r'%s'); from tools import single_instance; "
            "print(single_instance.acquire(r'%s'))" % (PROJECT_ROOT, name)
        )
        first = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True).stdout.strip()
        second = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True).stdout.strip()
        self.assertEqual((first, second), ("True", "True"), "程序結束後 Mutex 應自動釋放")


if __name__ == "__main__":
    unittest.main()
