"""scripts/sync_skills.py 單元測試 (使用暫存資料夾，不碰真正的技能庫)"""

import os
import shutil
import sys
import tempfile
import time
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(PROJECT_ROOT, "scripts"))

from tests import netguard  # noqa: E402,F401  測試禁止連外

import sync_skills  # noqa: E402


def write(path, text, mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    if mtime:
        os.utime(path, (mtime, mtime))


class TestSyncSkills(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="arq_skills_")
        self.dirs = [os.path.join(self.tmp, n) for n in ("claude", "agy", "agy_cli")]
        for d in self.dirs:
            os.makedirs(d)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_in_sync(self):
        for d in self.dirs:
            write(os.path.join(d, "tdd", "SKILL.md"), "same")
        self.assertEqual(sync_skills.plan_sync(self.dirs), [])
        self.assertEqual(sync_skills.main(["--dirs", *self.dirs]), 0)

    def test_missing_skill_is_added_everywhere(self):
        write(os.path.join(self.dirs[0], "new-skill", "SKILL.md"), "hello")
        actions = sync_skills.plan_sync(self.dirs)
        self.assertEqual({(a[0], a[1], a[3]) for a in actions},
                         {("add", "new-skill", self.dirs[1]), ("add", "new-skill", self.dirs[2])})
        self.assertEqual(sync_skills.main(["--dirs", *self.dirs]), 1)  # 只檢查：有差異回傳 1
        sync_skills.main(["--dirs", *self.dirs, "--apply"])
        for d in self.dirs:
            with open(os.path.join(d, "new-skill", "SKILL.md"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "hello")
        self.assertEqual(sync_skills.plan_sync(self.dirs), [])

    def test_newest_version_wins(self):
        old = time.time() - 3600
        write(os.path.join(self.dirs[0], "grill", "SKILL.md"), "old", mtime=old)
        write(os.path.join(self.dirs[1], "grill", "SKILL.md"), "new")
        write(os.path.join(self.dirs[2], "grill", "SKILL.md"), "old", mtime=old)
        sync_skills.apply_sync(sync_skills.plan_sync(self.dirs))
        for d in self.dirs:
            with open(os.path.join(d, "grill", "SKILL.md"), encoding="utf-8") as f:
                self.assertEqual(f.read(), "new")

    def test_never_deletes(self):
        write(os.path.join(self.dirs[2], "only-here", "SKILL.md"), "x")
        sync_skills.apply_sync(sync_skills.plan_sync(self.dirs))
        self.assertTrue(all(os.path.isdir(os.path.join(d, "only-here")) for d in self.dirs))


if __name__ == "__main__":
    unittest.main()
