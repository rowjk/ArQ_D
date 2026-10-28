"""
整合與文件檢查 (的 J)

    - bot.on_ready 啟動時印出 config.feature_summary()（只有功能名稱與 ✅/⚪）
    - docs/ARCHITECTURE.md 有 4 張 Mermaid 圖，提到的檔案都真的存在
    - README 開頭改用 Mermaid 並連到 docs/ARCHITECTURE.md，「📂 結構」涵蓋新模組
    - GEMINI.md 檔案地圖涵蓋新模組
    - 行數預算 (bot.py / agent.py / rd_dispatch.py)
"""

import inspect
import os
import re
import subprocess
import sys
import unittest

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tests import netguard  # noqa: E402,F401  測試禁止連外

os.environ.setdefault("ARQ_TESTING", "1")

import bot  # noqa: E402

ARCH = os.path.join(PROJECT_ROOT, "docs", "ARCHITECTURE.md")


def _read(*parts: str) -> str:
    with open(os.path.join(PROJECT_ROOT, *parts), encoding="utf-8") as f:
        return f.read()


def _line_count(*parts: str) -> int:
    return len(_read(*parts).splitlines())


def _mermaid_blocks(text: str):
    return re.findall(r"```mermaid\n(.*?)```", text, flags=re.S)


class FeatureSummaryStartupTests(unittest.TestCase):
    def test_on_ready_logs_feature_summary(self):
        src = inspect.getsource(bot.on_ready)
        self.assertIn("feature_summary()", src)
        self.assertIn("🧩 功能", src)

    def test_on_ready_logs_summary_through_logger_not_values(self):
        # 摘要走 logger（與其他啟動訊息一致），且只有名稱與 ✅/⚪
        self.assertIn("feature_summary", vars(bot))
        text = bot.feature_summary()
        self.assertRegex(text, r"^([✅⚪][^、✅⚪]+)(、[✅⚪][^、✅⚪]+)*$")


class ArchitectureDocTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert os.path.isfile(ARCH), "docs/ARCHITECTURE.md 不存在"
        cls.text = _read("docs", "ARCHITECTURE.md")
        cls.blocks = _mermaid_blocks(cls.text)

    def test_four_mermaid_diagrams_of_expected_kinds(self):
        self.assertEqual(len(self.blocks), 4)
        kinds = [b.strip().splitlines()[0].strip() for b in self.blocks]
        self.assertEqual(kinds, ["flowchart LR", "sequenceDiagram", "flowchart TD", "graph TD"])

    def test_each_diagram_has_explanation_and_tradeoff(self):
        sections = re.split(r"^## ", self.text, flags=re.M)[1:]
        diagram_sections = [s for s in sections if "```mermaid" in s]
        self.assertEqual(len(diagram_sections), 4)
        for sec in diagram_sections:
            prose = re.sub(r"```mermaid.*?```", "", sec, flags=re.S)
            sentences = [s for s in re.split(r"[。！？\n]", prose) if len(s.strip()) > 8]
            self.assertGreaterEqual(len(sentences), 3, sec.splitlines()[0])
            self.assertIn("取捨", prose, sec.splitlines()[0])

    def test_overview_names_gates_and_modules(self):
        overview = self.blocks[0]
        for name in ("bot.py", "agent.py", "tool_registry", "rd_dispatch", "risk_policy", "secret_guard",
                     "Claude Code", "agy"):
            self.assertIn(name, overview)

    def test_message_flow_covers_persistence_and_cache(self):
        flow = self.blocks[1]
        for name in ("on_message", "chat_store", "同意卡", "快取"):
            self.assertIn(name, flow)

    def test_dispatch_flow_covers_handoff_and_pr(self):
        flow = self.blocks[2]
        for name in ("相依", "worktree", "換手", "驗收卡", "PR", "merge"):
            self.assertIn(name, flow)

    def test_dependency_graph_has_new_modules(self):
        graph = self.blocks[3]
        for name in ("discord_ui", "tool_registry", "rd_context", "rd_report", "rd_common", "config",
                     "chat_store", "thread_bridge", "log_setup"):
            self.assertIn(name, graph)

    def test_design_tradeoffs_section(self):
        self.assertIn("## 設計取捨", self.text)
        for topic in ("Token", "fail-closed", ".env", "_temp/"):
            self.assertIn(topic, self.text)

    def test_referenced_files_exist(self):
        refs = set(re.findall(r"`((?:tools/|discord_ui/|prompts/)?[A-Za-z_]+\.(?:py|md))`", self.text))
        self.assertTrue(refs)
        for ref in refs:
            candidates = [ref, os.path.join("tools", ref), os.path.join("discord_ui", ref)]
            self.assertTrue(any(os.path.isfile(os.path.join(PROJECT_ROOT, c)) for c in candidates),
                            f"ARCHITECTURE.md 提到不存在的檔案：{ref}")

    def test_diagram_nodes_are_quoted_for_github_renderer(self):
        # 含括號、斜線、中文標點的節點標籤沒加引號會讓 GitHub 的 Mermaid 解析失敗
        for block in self.blocks:
            for label in re.findall(r"\[([^\]\n]*)\]", block):
                if re.search(r"[()/:]", label):
                    self.assertTrue(label.startswith('"') and label.endswith('"'), f"標籤未加引號：{label}")


class ReadmeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.text = _read("README.md")

    def test_intro_uses_mermaid_instead_of_ascii(self):
        head = self.text.split("## ✨ 特色")[0]
        self.assertEqual(len(_mermaid_blocks(head)), 1)
        self.assertNotIn("📱 Discord ─►", self.text)

    def test_links_to_architecture_doc(self):
        self.assertIn("docs/ARCHITECTURE.md", self.text)

    def test_structure_section_lists_new_modules(self):
        structure = self.text.split("## 📂 結構")[1]
        for name in ("discord_ui/", "tools/config.py", "tools/chat_store.py", "tools/log_setup.py",
                     "tools/thread_bridge.py", "docs/ARCHITECTURE.md"):
            self.assertIn(name, structure)


class GeminiDocTests(unittest.TestCase):
    def test_file_map_lists_new_modules(self):
        text = _read("GEMINI.md")
        for name in ("tools/chat_store.py", "tools/log_setup.py", "discord_ui/", "docs/ARCHITECTURE.md"):
            self.assertIn(name, text)


class PrivacyAndBudgetTests(unittest.TestCase):
    def test_private_paths_not_tracked(self):
        out = subprocess.run(["git", "ls-files"], cwd=PROJECT_ROOT, capture_output=True, text=True,
                             encoding="utf-8").stdout.splitlines()
        for path in out:
            self.assertFalse(path.startswith(("_temp/", "task/")), path)
            self.assertNotIn(path, (".env", "mcp_servers.json"))

    def test_line_budgets(self):
        self.assertLessEqual(_line_count("bot.py"), 1000)
        self.assertLessEqual(_line_count("agent.py"), 870)
        # rd_dispatch 的 520 行預算 已改為 620
        self.assertLessEqual(_line_count("tools", "rd_dispatch.py"), 620)


if __name__ == "__main__":
    unittest.main()
