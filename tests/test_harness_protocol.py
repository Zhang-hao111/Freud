"""回归测试：guardrail 拦截 / 工具异常后，会话必须保持 OpenAI 工具调用协议有效。

协议要求：assistant 消息携带 tool_calls 时，其后必须跟带对应 tool_call_id 的
ToolMessage，否则下一次 Chat Completions 调用会被 400 拒绝
（"An assistant message with 'tool_calls' must be followed by tool messages
responding to each 'tool_call_id'"）。

运行方式（项目根目录）：
    .venv/bin/python -m unittest discover -s tests -v
"""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.types import Action, AssistantMessage, ToolMessage
from agent.core import run_task
from agent.llm import MockLLM
from agent.registry import create_default_registry
from agent.memory import FileMemory
from agent.tracer import Tracer

FORK_BOMB = ':(){ :|:& };:'


class ProtocolCheckingLLM:
    """包装 MockLLM，每次调用前校验历史消息满足工具调用协议。"""

    def __init__(self, inner):
        self.inner = inner
        self.violations = []

    def chat(self, messages, tools):
        open_calls = set()
        for m in messages:
            if isinstance(m, AssistantMessage) and m.tool_calls:
                for tc in m.tool_calls:
                    tc_id = tc['id'] if isinstance(tc, dict) else tc.id
                    open_calls.add(tc_id)
            elif isinstance(m, ToolMessage):
                if m.tool_call_id in open_calls:
                    open_calls.discard(m.tool_call_id)
                else:
                    self.violations.append(
                        f'ToolMessage 响应了未发出的 tool_call_id: {m.tool_call_id!r}')
        if open_calls:
            self.violations.append(
                f'assistant 的 tool_calls 未被 ToolMessage 响应: {sorted(open_calls)}')
        return self.inner.chat(messages, tools)


class ProtocolRegressionTest(unittest.TestCase):
    def _run(self, responses):
        llm = ProtocolCheckingLLM(MockLLM(responses=responses))
        registry = create_default_registry()
        with tempfile.TemporaryDirectory() as tmp:
            memory = FileMemory(f'{tmp}/memory.json')
            tracer = Tracer(f'{tmp}/traces')
            answer = run_task(
                goal='测试任务', task_name='回归测试', llm=llm,
                tool_registry=registry, memory=memory, tracer=tracer,
                max_steps=10, workspace=tmp,
            )
        return answer, llm.violations

    def test_deny_keeps_protocol_valid(self):
        """fork 炸弹被 deny 后，循环必须能继续且会话协议不被破坏。"""
        responses = [
            Action(type='call_tool', tool='shell',
                   args={'command': FORK_BOMB}, tool_call_id='t1'),
            Action(type='done', answer='已改用安全方式', tool_call_id='t2'),
        ]
        answer, violations = self._run(responses)
        self.assertEqual(violations, [])
        self.assertEqual(answer, '已改用安全方式')

    def test_escalate_denied_keeps_protocol_valid(self):
        """rm -rf / 触发 escalate，approver 缺省拒绝后协议不被破坏。"""
        responses = [
            Action(type='call_tool', tool='shell',
                   args={'command': 'rm -rf /'}, tool_call_id='t1'),
            Action(type='done', answer='ok', tool_call_id='t2'),
        ]
        answer, violations = self._run(responses)
        self.assertEqual(violations, [])

    def test_unknown_tool_exception_keeps_protocol_valid(self):
        """调用不存在的工具抛 ValueError 后协议不被破坏。"""
        responses = [
            Action(type='call_tool', tool='no_such_tool', args={},
                   tool_call_id='t1'),
            Action(type='done', answer='ok', tool_call_id='t2'),
        ]
        answer, violations = self._run(responses)
        self.assertEqual(violations, [])

    def test_normal_tool_flow_unchanged(self):
        """正常流程（安全命令 + done）不受影响。"""
        responses = [
            Action(type='call_tool', tool='shell',
                   args={'command': 'echo hello'}, tool_call_id='t1'),
            Action(type='done', answer='ok', tool_call_id='t2'),
        ]
        answer, violations = self._run(responses)
        self.assertEqual(violations, [])
        self.assertEqual(answer, 'ok')


if __name__ == '__main__':
    unittest.main()
