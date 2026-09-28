"""agent/ui 单元测试 — 颜色降级、中文对齐、渲染器（阶段四）。"""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import ui


class ColorEnableTest(unittest.TestCase):
    def tearDown(self):
        ui.set_enabled(None)

    def test_paint_disabled_returns_plain(self):
        ui.set_enabled(False)
        self.assertEqual(ui.paint('red', 'x'), 'x')
        self.assertEqual(ui.dot(), '⏺')

    def test_paint_enabled_wraps_ansi(self):
        ui.set_enabled(True)
        self.assertIn('\x1b[', ui.paint('red', 'x'))
        self.assertTrue(ui.paint('red', 'x').endswith('\x1b[0m'))

    def test_enabled_path_all_renderers(self):
        """颜色启用时所有渲染器都必须可调用（回归：_wrap 未定义只在 TTY 下触发）。"""
        ui.set_enabled(True)
        self.assertIn('\x1b[', ui.paint_accent('██'))
        self.assertIn('\x1b[', ui.dot())
        self.assertIn('\x1b[', ui.elbow())
        self.assertIn('\x1b[', ui.render_tool_line('shell', {'command': 'ls'}))
        self.assertIn('\x1b[', ui.render_result_line('ok', True))

    def test_accent_is_pink_256_fallback(self):
        ui.set_enabled(True)
        with mock.patch.dict('os.environ', {'COLORTERM': ''}):
            self.assertIn('38;5;205', ui.dot())
        with mock.patch.dict('os.environ', {'COLORTERM': 'truecolor'}):
            self.assertIn('38;2;255;105;180', ui.dot())


class AmbiguousWidthTest(unittest.TestCase):
    def test_cjk_locale_counts_blocks_as_wide(self):
        with mock.patch.dict('os.environ', {'LANG': 'zh_CN.UTF-8', 'LC_ALL': '', 'LC_CTYPE': ''}):
            ui._ambig_wide_cache = None  # 重置缓存
            self.assertEqual(ui.visual_width('██'), 4)

    def test_latin_locale_counts_blocks_as_narrow(self):
        with mock.patch.dict('os.environ', {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_CTYPE': ''}):
            ui._ambig_wide_cache = None
            self.assertEqual(ui.visual_width('██'), 2)

    def test_cjk_wide_chars_always_two(self):
        with mock.patch.dict('os.environ', {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_CTYPE': ''}):
            ui._ambig_wide_cache = None
            self.assertEqual(ui.visual_width('中'), 2)
        ui._ambig_wide_cache = None

    def tearDown(self):
        ui._ambig_wide_cache = None  # 恢复真实环境缓存

    def test_detect_no_color_env(self):
        ui.set_enabled(None)
        fake_tty = SimpleNamespace(isatty=lambda: True)
        with mock.patch.dict('os.environ', {'NO_COLOR': '1'}), \
             mock.patch.object(ui.sys, 'stdout', fake_tty):
            self.assertFalse(ui.detect_enabled())

    def test_detect_non_tty(self):
        ui.set_enabled(None)
        fake_pipe = SimpleNamespace(isatty=lambda: False)
        with mock.patch.dict('os.environ', {'NO_COLOR': ''}, clear=False), \
             mock.patch.object(ui.sys, 'stdout', fake_pipe):
            self.assertFalse(ui.detect_enabled())


class WidthTest(unittest.TestCase):
    def test_visual_width(self):
        self.assertEqual(ui.visual_width('abc'), 3)
        self.assertEqual(ui.visual_width('中a'), 3)
        self.assertEqual(ui.visual_width('中文'), 4)

    def test_pad_aligns_by_display_width(self):
        padded = ui.pad('中文x', 8)
        self.assertEqual(ui.visual_width(padded), 8)


class RendererTest(unittest.TestCase):
    def tearDown(self):
        ui.set_enabled(None)

    def test_tool_display_name(self):
        self.assertEqual(ui.tool_display_name('shell'), 'Bash')
        self.assertEqual(ui.tool_display_name('read_file'), 'Read')
        self.assertEqual(ui.tool_display_name('custom'), 'custom')

    def test_render_tool_line(self):
        ui.set_enabled(False)
        line = ui.render_tool_line('shell', {'command': 'ls -la'})
        self.assertTrue(line.startswith('⏺ '))
        self.assertIn('Bash(', line)
        self.assertIn('ls -la', line)

    def test_render_tool_line_truncates(self):
        ui.set_enabled(False)
        line = ui.render_tool_line('shell', {'command': 'x' * 500})
        self.assertIn('…', line)

    def test_render_result_collapses_newlines(self):
        ui.set_enabled(False)
        line = ui.render_result_line('a\n\n  b\nc', ok=True)
        self.assertNotIn('\n', line.replace('⎿', ''))
        self.assertIn('a b c', line)

    def test_render_result_truncates_with_suffix(self):
        ui.set_enabled(False)
        line = ui.render_result_line('x' * 5000, ok=True, limit=200)
        self.assertIn('…(+', line)

    def test_render_result_error_uses_plain_marker_when_disabled(self):
        ui.set_enabled(False)
        line = ui.render_result_line('出错啦', ok=False)
        self.assertIn('⎿', line)
        self.assertIn('出错啦', line)


if __name__ == '__main__':
    unittest.main()
