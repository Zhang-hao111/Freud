"""CLI 自然语言/斜杠命令解析单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.cli import _parse_intent


class ParseIntentTest(unittest.TestCase):
    def test_slash_key(self):
        self.assertEqual(
            _parse_intent('/key = sk-abc'),
            {'type': 'set', 'key': 'api_key', 'value': 'sk-abc'})

    def test_natural_language_set_model(self):
        r = _parse_intent('把 model 改成 gpt-4o')
        self.assertEqual((r['type'], r['key'], r['value']), ('set', 'model', 'gpt-4o'))

    def test_slash_run(self):
        self.assertEqual(_parse_intent('/run task.md'), {'type': 'run', 'file': 'task.md'})

    def test_exit_and_help(self):
        self.assertEqual(_parse_intent('/exit'), {'type': 'exit'})
        self.assertEqual(_parse_intent('帮助'), {'type': 'help'})

    def test_chat_fallback(self):
        self.assertEqual(
            _parse_intent('实现一个斐波那契数列'),
            {'type': 'chat', 'text': '实现一个斐波那契数列'})


if __name__ == '__main__':
    unittest.main()
