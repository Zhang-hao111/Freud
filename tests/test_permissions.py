import tempfile
import unittest
from pathlib import Path

from agent.core import run_task
from agent.memory import FileMemory
from agent.permissions import MODES, auto_approve_escalate, cycle, needs_confirm
from agent.registry import ToolRegistry
from agent.tracer import Tracer
from agent.types import Action, BaseTool, LLMResponse


class ScriptedLLM:
    """按脚本返回 LLMResponse，并记录每轮收到的 messages。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.n = 0
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append(list(messages))
        r = self.responses[min(self.n, len(self.responses) - 1)]
        self.n += 1
        return r


class RecordingShell(BaseTool):
    """替身 shell：只记录命令不执行，用于安全地测 escalate 路径。"""

    name = 'shell'
    description = 'record-only shell'
    parameters = {'type': 'object', 'properties': {'command': {'type': 'string'}}}

    def __init__(self):
        self.commands = []

    def execute(self, args):
        self.commands.append(args.get('command', ''))
        return type('ToolResult', (), {'success': True, 'data': 'ok', 'error': ''})()


def _call(tool, args, call_id='t1'):
    return Action(type='call_tool', tool=tool, args=args, tool_call_id=call_id)


def _done(call_id='t2'):
    return Action(type='done', answer='ok', tool_call_id=call_id)


class PermissionsUnitTest(unittest.TestCase):
    def test_needs_confirm_covers_both_edit_tools(self):
        self.assertTrue(needs_confirm('ask', 'write_file'))
        self.assertTrue(needs_confirm('ask', 'edit_file'))
        self.assertFalse(needs_confirm('ask', 'shell'))
        self.assertFalse(needs_confirm('ask', 'grep'))
        self.assertFalse(needs_confirm('accept', 'write_file'))
        self.assertFalse(needs_confirm('accept', 'edit_file'))
        self.assertFalse(needs_confirm('yolo', 'write_file'))

    def test_cycle_and_escalate(self):
        self.assertEqual(cycle('ask'), 'accept')
        self.assertEqual(cycle('yolo'), 'ask')
        self.assertEqual(MODES, ('ask', 'accept', 'yolo'))
        self.assertTrue(auto_approve_escalate('yolo'))
        self.assertFalse(auto_approve_escalate('ask'))
        self.assertFalse(auto_approve_escalate('accept'))


class RunAgentModeTest(unittest.TestCase):
    def _run(self, tmp, responses, **kwargs):
        registry = ToolRegistry()
        shell = RecordingShell()
        registry.register(shell)
        llm = ScriptedLLM(responses)
        answer = run_task(goal='g', task_name='t', llm=llm,
                           tool_registry=registry,
                           memory=FileMemory(f'{tmp}/m.json'),
                           tracer=Tracer(f'{tmp}/traces'),
                           max_steps=5, workspace=tmp, **kwargs)
        return answer, llm, shell

    def test_ask_without_confirmer_denies_file_edit(self):
        """非交互 ask 模式下 edit_file 无法确认 → 拒绝执行并提示用 --yes。"""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'new.txt'
            answer, llm, _ = self._run(tmp, [
                LLMResponse(actions=[_call('edit_file', {'path': str(target), 'old_string': 'a', 'new_string': 'b'})]),
                LLMResponse(actions=[_done()]),
            ])
            self.assertEqual(answer, 'ok')
            self.assertFalse(target.exists())
            deny = [m.content for m in llm.seen[1] if type(m).__name__ == 'ToolMessage']
            self.assertTrue(any('--yes' in c for c in deny), deny)

    def test_ask_with_confirmer_applies_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'edit.txt'
            target.write_text('hello world', encoding='utf-8')
            answer, llm, _ = self._run(tmp, [
                LLMResponse(actions=[_call('edit_file', {'path': str(target), 'old_string': 'hello', 'new_string': 'bye'})]),
                LLMResponse(actions=[_done()]),
            ], mode='ask', confirmer=lambda action: False)
            self.assertEqual(answer, 'ok')
            self.assertIn('hello', target.read_text(encoding='utf-8'))  # 未被修改
            deny = [m.content for m in llm.seen[1] if type(m).__name__ == 'ToolMessage']
            self.assertTrue(any('用户拒绝本次文件修改' in c for c in deny), deny)

            llm2 = ScriptedLLM([
                LLMResponse(actions=[_call('edit_file', {'path': str(target), 'old_string': 'hello', 'new_string': 'bye'})]),
                LLMResponse(actions=[_done()]),
            ])
            from agent.registry import create_default_registry
            run_task(goal='g', task_name='t', llm=llm2,
                      tool_registry=create_default_registry(),
                      memory=FileMemory(f'{tmp}/m2.json'),
                      tracer=Tracer(f'{tmp}/traces2'),
                      max_steps=5, workspace=tmp,
                      mode='ask', confirmer=lambda action: True)
            self.assertIn('bye', target.read_text(encoding='utf-8'))

    def test_accept_mode_executes_edits_without_confirmer(self):
        with tempfile.TemporaryDirectory() as tmp:
            from agent.registry import create_default_registry
            target = Path(tmp) / 'w.txt'
            llm = ScriptedLLM([
                LLMResponse(actions=[_call('write_file', {'path': str(target), 'content': 'data'})]),
                LLMResponse(actions=[_done()]),
            ])
            run_task(goal='g', task_name='t', llm=llm,
                      tool_registry=create_default_registry(),
                      memory=FileMemory(f'{tmp}/m.json'),
                      tracer=Tracer(f'{tmp}/traces'),
                      max_steps=5, workspace=tmp, mode='accept')
            self.assertEqual(target.read_text(encoding='utf-8'), 'data')

    def test_yolo_auto_approves_escalate_without_approver(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer, _, shell = self._run(tmp, [
                LLMResponse(actions=[_call('shell', {'command': 'fdisk -l'})]),
                LLMResponse(actions=[_done()]),
            ], mode='yolo')
            self.assertEqual(answer, 'ok')
            self.assertEqual(shell.commands, ['fdisk -l'])

    def test_ask_denies_escalate_without_approver(self):
        with tempfile.TemporaryDirectory() as tmp:
            answer, llm, shell = self._run(tmp, [
                LLMResponse(actions=[_call('shell', {'command': 'fdisk -l'})]),
                LLMResponse(actions=[_done()]),
            ])
            self.assertEqual(answer, 'ok')
            self.assertEqual(shell.commands, [])
            deny = [m.content for m in llm.seen[1] if type(m).__name__ == 'ToolMessage']
            self.assertTrue(any('操作被拒绝' in c for c in deny), deny)

    def test_invalid_mode_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._run(tmp, [LLMResponse(actions=[_done()])], mode='sudo')


if __name__ == '__main__':
    unittest.main()
