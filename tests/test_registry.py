import os
import tempfile
import unittest
from pathlib import Path

from agent.registry import EditFileTool, GrepTool, create_default_registry


class EditFileToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.file = self.directory / "sample.py"
        self.file.write_text("def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n",
                            encoding="utf-8")

    def test_unique_replacement(self):
        result = EditFileTool().execute({"path": str(self.file), "old_string": "return a + b", "new_string": "return a + b  # sum"})
        self.assertTrue(result.success, result.error)
        self.assertIn("已替换 1 处", result.data)
        self.assertIn("return a + b  # sum", self.file.read_text(encoding="utf-8"))

    def test_rejects_ambiguous_match_without_replace_all(self):
        result = EditFileTool().execute({"path": str(self.file), "old_string": "return a", "new_string": "return x"})
        self.assertFalse(result.success)
        self.assertIn("出现 2 次", result.error)

    def test_replace_all_replaces_every_match(self):
        result = EditFileTool().execute({"path": str(self.file), "old_string": "def ", "new_string": "async def ", "replace_all": True})
        self.assertTrue(result.success, result.error)
        self.assertEqual(self.file.read_text(encoding="utf-8").count("async def "), 2)

    def test_rejects_missing_old_string(self):
        result = EditFileTool().execute({"path": str(self.file), "old_string": "not in file", "new_string": "x"})
        self.assertFalse(result.success)
        self.assertIn("未找到 old_string", result.error)

    def test_rejects_identical_strings_and_empty_old_string(self):
        same = EditFileTool().execute({"path": str(self.file), "old_string": "return a", "new_string": "return a"})
        empty = EditFileTool().execute({"path": str(self.file), "old_string": "", "new_string": "x"})
        self.assertFalse(same.success)
        self.assertFalse(empty.success)

    def test_does_not_create_files(self):
        result = EditFileTool().execute({"path": str(self.directory / "missing.py"), "old_string": "a", "new_string": "b"})
        self.assertFalse(result.success)
        self.assertIn("文件不存在", result.error)


class GrepToolTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        (self.directory / "app.py").write_text("import os\nTOKEN = 'abc'\nprint(TOKEN)\n", encoding="utf-8")
        (self.directory / "util.py").write_text("token = 'hidden'\n", encoding="utf-8")
        nested = self.directory / "pkg" / "deep"
        nested.mkdir(parents=True)
        (nested / "core.py").write_text("def token_limit():\n    pass\n", encoding="utf-8")
        skipped = self.directory / ".git"
        skipped.mkdir()
        (skipped / "config.py").write_text("TOKEN = 'should-not-appear'\n", encoding="utf-8")
        (self.directory / "binary.dat").write_bytes(b"TOKEN\0\0\0")

    def test_recursive_search_reports_file_line_text(self):
        result = GrepTool().execute({"pattern": r"TOKEN|token", "path": str(self.directory)})
        self.assertTrue(result.success, result.error)
        self.assertIn("app.py:2:", result.data)
        self.assertIn("util.py:1:", result.data)
        self.assertNotIn("should-not-appear", result.data)
        self.assertNotIn("binary.dat", result.data)

    def test_include_filter_and_ignore_case(self):
        only_util = GrepTool().execute({"pattern": "token", "path": str(self.directory), "include": "util.py"})
        self.assertTrue(only_util.success)
        self.assertIn("util.py:1:", only_util.data)
        self.assertNotIn("app.py", only_util.data)
        case_sensitive = GrepTool().execute({"pattern": "token", "path": str(self.directory), "include": "app.py"})
        self.assertEqual(case_sensitive.data, "未找到匹配")
        insensitive = GrepTool().execute({"pattern": "token", "path": str(self.directory), "include": "app.py", "ignore_case": True})
        self.assertIn("app.py:2:", insensitive.data)

    def test_single_file_search(self):
        result = GrepTool().execute({"pattern": "def ", "path": str(self.directory / "pkg" / "deep" / "core.py")})
        self.assertTrue(result.success)
        self.assertIn("def token_limit", result.data)

    def test_invalid_regex_and_missing_path(self):
        bad = GrepTool().execute({"pattern": "("})
        self.assertFalse(bad.success)
        self.assertIn("无效的正则表达式", bad.error)
        missing = GrepTool().execute({"pattern": "x", "path": str(self.directory / "nope")})
        self.assertFalse(missing.success)
        self.assertIn("路径不存在", missing.error)

    def test_default_registry_contains_new_tools(self):
        names = {tool.name for tool in create_default_registry().list()}
        self.assertLessEqual({"read_file", "write_file", "edit_file", "grep", "shell"}, names)


if __name__ == "__main__":
    unittest.main()
