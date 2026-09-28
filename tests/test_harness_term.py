"""权限模式与终端交互层测试（阶段六）。"""
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import term, ui
from agent.permissions import (
    MODES, auto_approve_escalate, chip, cycle, needs_confirm,
)
from agent.term import render_footer


def _plain(text: str) -> str:
    return re.compile(r'\x1b\[[0-9;]*[A-Za-z]').sub('', text)


class PermissionsTest(unittest.TestCase):
    def test_cycle_order(self):
        self.assertEqual(cycle('ask'), 'accept')
        self.assertEqual(cycle('accept'), 'yolo')
        self.assertEqual(cycle('yolo'), 'ask')
        self.assertEqual(cycle('unknown'), 'accept')  # 非法值视为 ask

    def test_modes_complete(self):
        self.assertEqual(MODES, ('ask', 'accept', 'yolo'))

    def test_needs_confirm(self):
        self.assertTrue(needs_confirm('ask', 'write_file'))
        self.assertFalse(needs_confirm('ask', 'shell'))
        self.assertFalse(needs_confirm('ask', 'read_file'))
        self.assertFalse(needs_confirm('accept', 'write_file'))
        self.assertFalse(needs_confirm('yolo', 'write_file'))

    def test_auto_approve_escalate(self):
        self.assertFalse(auto_approve_escalate('ask'))
        self.assertFalse(auto_approve_escalate('accept'))
        self.assertTrue(auto_approve_escalate('yolo'))

    def test_chip_has_text_and_style(self):
        for mode in MODES:
            text, style = chip(mode)
            self.assertTrue(text)
            self.assertIn(style, ('yellow', 'green', 'red'))


class FooterTest(unittest.TestCase):
    def test_footer_has_no_fill_line(self):
        """footer 只含内容段，不再有右侧填充横线。"""
        segs = [('⏵⏵ ask before edits', 'yellow'), ('shift+tab 切换模式', 'dim'), ('deepseek-chat', 'dim')]
        plain = _plain(render_footer(segs[:-1], segs[-1:]))
        self.assertIn('⏵⏵ ask before edits', plain)
        self.assertNotIn('─', plain)

    def test_footer_left_only(self):
        out = _plain(render_footer([('only', 'dim')]))
        self.assertIn('only', out)
        self.assertNotIn('─', out)


class ChooseFallbackTest(unittest.TestCase):
    def test_non_tty_numeric_select(self):
        """非 TTY 下降级为输入序号。"""
        with mock.patch.object(term.sys.stdin, 'isatty', return_value=False), \
             mock.patch('builtins.input', return_value='2'):
            self.assertEqual(term.choose(['a', 'b', 'c']), 1)

    def test_non_tty_cancel(self):
        with mock.patch.object(term.sys.stdin, 'isatty', return_value=False), \
             mock.patch('builtins.input', return_value=''):
            self.assertIsNone(term.choose(['a', 'b']))

    def test_non_tty_empty_options(self):
        self.assertIsNone(term.choose([]))


class VisualClipTest(unittest.TestCase):
    def test_clip_cjk(self):
        self.assertEqual(ui.visual_clip('中文abc', 5), '中文a')
        self.assertEqual(ui.visual_clip('abc', 10), 'abc')
        self.assertEqual(ui.visual_clip('中', 1), '')


if __name__ == '__main__':
    unittest.main()
