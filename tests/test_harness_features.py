"""功能特性测试：重试、记忆注入、并行 tool calls、输出截断、escalate 批准。"""
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import agent.llm as llm_mod
from agent.llm import OpenAIProvider
from agent.types import (
    Action, AssistantMessage, ToolMessage, SystemMessage, UserMessage, LLMResponse,
)
from agent.core import run_task, build_memory_block
from agent.registry import create_default_registry, ReadFileTool
from agent.memory import FileMemory
from agent.tracer import Tracer


class MessageCapturingLLM:
    """记录每次收到的 messages，按脚本返回 LLMResponse。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.n = 0
        self.seen = []

    def chat(self, messages, tools):
        self.seen.append(list(messages))
        r = self.responses[min(self.n, len(self.responses) - 1)]
        self.n += 1
        return r


def _fake_api_response(content='hi'):
    msg = SimpleNamespace(content=content, tool_calls=None)
    return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


class RetryTest(unittest.TestCase):
    def _provider(self, create_fn):
        p = OpenAIProvider(api_key='sk-test')
        p.client = SimpleNamespace(
            chat=SimpleNamespace(completions=SimpleNamespace(create=create_fn)))
        return p

    def _no_sleep(self):
        orig = llm_mod.time.sleep
        llm_mod.time.sleep = lambda s: None
        return orig

    def test_retry_then_success(self):
        """前两次失败第三次成功：应重试且不抛异常。"""
        calls = {'n': 0}

        def flaky(*a, **kw):
            calls['n'] += 1
            if calls['n'] < 3:
                raise RuntimeError('transient')
            return _fake_api_response()

        orig = self._no_sleep()
        try:
            resp = self._provider(flaky).chat([UserMessage(content='hi')], [])
        finally:
            llm_mod.time.sleep = orig
        self.assertEqual(calls['n'], 3)
        self.assertEqual(resp.message.content, 'hi')

    def test_retry_exhausted_raises(self):
        calls = {'n': 0}

        def always_fail(*a, **kw):
            calls['n'] += 1
            raise RuntimeError('down')

        orig = self._no_sleep()
        try:
            with self.assertRaises(RuntimeError):
                self._provider(always_fail).chat([UserMessage(content='hi')], [])
        finally:
            llm_mod.time.sleep = orig
        self.assertEqual(calls['n'], OpenAIProvider.MAX_RETRIES)


class MemoryInjectionTest(unittest.TestCase):
    def test_memory_visible_in_system_prompt(self):
        """写入的记忆必须出现在下一轮任务的系统提示中（写读闭环）。"""
        with tempfile.TemporaryDirectory() as tmp:
            mem = FileMemory(f'{tmp}/m.json')
            mem.write('用户偏好', '使用 Python 实现')

            llm = MessageCapturingLLM([
                LLMResponse(actions=[Action(type='done', answer='ok', tool_call_id='t1')]),
            ])
            run_task(goal='g', task_name='t', llm=llm,
                      tool_registry=create_default_registry(),
                      memory=mem, tracer=Tracer(f'{tmp}/traces'),
                      max_steps=5, workspace=tmp)

            sys_msg = llm.seen[0][0]
            self.assertIsInstance(sys_msg, SystemMessage)
            self.assertIn('用户偏好', sys_msg.content)
            self.assertIn('使用 Python 实现', sys_msg.content)

    def test_build_memory_block_empty_and_none(self):
        self.assertEqual(build_memory_block(None), '')
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(build_memory_block(FileMemory(f'{tmp}/none.json')), '')


class MultiToolCallTest(unittest.TestCase):
    def test_parallel_tool_calls_all_answered(self):
        """一次 LLM 响应携带两个并行 read_file：都必须执行并各自以 ToolMessage 响应。"""
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / 'a.txt').write_text('AAA')
            (Path(tmp) / 'b.txt').write_text('BBB')

            resp1 = LLMResponse(
                actions=[
                    Action(type='call_tool', tool='read_file',
                           args={'path': f'{tmp}/a.txt'}, tool_call_id='t1'),
                    Action(type='call_tool', tool='read_file',
                           args={'path': f'{tmp}/b.txt'}, tool_call_id='t2'),
                ],
                message=AssistantMessage(content=None, tool_calls=[
                    {'id': 't1', 'type': 'function',
                     'function': {'name': 'read_file', 'arguments': '{}'}},
                    {'id': 't2', 'type': 'function',
                     'function': {'name': 'read_file', 'arguments': '{}'}},
                ]),
            )
            resp2 = LLMResponse(
                actions=[Action(type='done', answer='ok', tool_call_id='t3')],
                message=AssistantMessage(content=None, tool_calls=[
                    {'id': 't3', 'type': 'function',
                     'function': {'name': 'done', 'arguments': '{}'}},
                ]),
            )
            llm = MessageCapturingLLM([resp1, resp2])
            answer = run_task(goal='g', task_name='t', llm=llm,
                               tool_registry=create_default_registry(),
                               memory=FileMemory(f'{tmp}/m.json'),
                               tracer=Tracer(f'{tmp}/traces'),
                               max_steps=5, workspace=tmp)

            # 第二次 LLM 调用时，t1/t2 必须都已被 ToolMessage 响应
            tool_ids = [m.tool_call_id for m in llm.seen[1]
                        if isinstance(m, ToolMessage)]
            self.assertEqual(sorted(tool_ids), ['t1', 't2'])
            contents = [m.content for m in llm.seen[1] if isinstance(m, ToolMessage)]
            self.assertTrue(any('AAA' in c for c in contents))
            self.assertTrue(any('BBB' in c for c in contents))
            self.assertEqual(answer, 'ok')


class TruncationTest(unittest.TestCase):
    def test_read_file_output_clipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'big.txt'
            p.write_text('x' * 30000)
            r = ReadFileTool().execute({'path': str(p)})
            self.assertTrue(r.success)
            self.assertLess(len(r.data), 30000)
            self.assertIn('截断', r.data)


class EscalateApproverTest(unittest.TestCase):
    def _run(self, tmp, approver):
        resp1 = LLMResponse(
            actions=[
                # fdisk -l 命中 escalate 模式且本身无害
                Action(type='call_tool', tool='shell',
                       args={'command': 'fdisk -l'}, tool_call_id='t1'),
            ],
            message=AssistantMessage(content=None, tool_calls=[
                {'id': 't1', 'type': 'function',
                 'function': {'name': 'shell', 'arguments': '{}'}},
            ]),
        )
        resp2 = LLMResponse(
            actions=[Action(type='done', answer='ok', tool_call_id='t2')],
            message=AssistantMessage(content=None, tool_calls=[
                {'id': 't2', 'type': 'function',
                 'function': {'name': 'done', 'arguments': '{}'}},
            ]),
        )
        llm = MessageCapturingLLM([resp1, resp2])
        answer = run_task(goal='g', task_name='t', llm=llm,
                           tool_registry=create_default_registry(),
                           memory=FileMemory(f'{tmp}/m.json'),
                           tracer=Tracer(f'{tmp}/traces'),
                           max_steps=5, workspace=tmp,
                           approver=approver)
        return llm, answer

    def test_approved_escalate_executes(self):
        """approver 批准后 escalate 命令应执行且协议不被破坏。"""
        with tempfile.TemporaryDirectory() as tmp:
            llm, answer = self._run(tmp, approver=lambda action: True)
            self.assertEqual(answer, 'ok')
            tool_ids = [m.tool_call_id for m in llm.seen[1] if isinstance(m, ToolMessage)]
            self.assertEqual(tool_ids, ['t1'])

    def test_denied_escalate_rejected(self):
        """approver 拒绝后命令不执行，拒绝原因回灌且协议保持有效。"""
        with tempfile.TemporaryDirectory() as tmp:
            llm, answer = self._run(tmp, approver=lambda action: False)
            self.assertEqual(answer, 'ok')
            tool_msgs = [m for m in llm.seen[1] if isinstance(m, ToolMessage)]
            self.assertEqual(len(tool_msgs), 1)
            self.assertIn('操作被拒绝', tool_msgs[0].content)


if __name__ == '__main__':
    unittest.main()
