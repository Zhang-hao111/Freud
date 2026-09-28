"""Guardrail 三态分类单元测试。"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.guardrail import guardrail
from agent.types import Action


def shell_action(command: str) -> Action:
    return Action(type='call_tool', tool='shell', args={'command': command})


class GuardrailTest(unittest.TestCase):
    def test_allow_safe_command(self):
        self.assertEqual(guardrail(shell_action('echo hi')).disposition, 'allow')
        self.assertEqual(guardrail(shell_action('python test.py')).disposition, 'allow')

    def test_deny_fork_bomb(self):
        r = guardrail(shell_action(':(){ :|:& };:'))
        self.assertEqual(r.disposition, 'deny')

    def test_escalate_destructive(self):
        for cmd in ('rm -rf /', 'mkfs /dev/sda', 'dd if=/dev/zero of=/dev/sda', 'fdisk -l'):
            r = guardrail(shell_action(cmd))
            self.assertEqual(r.disposition, 'escalate', f'命令 {cmd!r} 应触发 escalate')

    def test_non_shell_tool_allowed(self):
        r = guardrail(Action(type='call_tool', tool='read_file', args={'path': 'x'}))
        self.assertEqual(r.disposition, 'allow')

    def test_done_and_take_note_not_blocked(self):
        self.assertEqual(guardrail(Action(type='done', answer='x')).disposition, 'allow')
        self.assertEqual(
            guardrail(Action(type='take_note', note_key='k', note_value='v')).disposition,
            'allow')


if __name__ == '__main__':
    unittest.main()
