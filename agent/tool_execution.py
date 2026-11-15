"""共享工具管线。确认的交互由调用入口提供。"""

from dataclasses import dataclass
from typing import Literal

from agent.guardrail import guardrail
from agent.permissions import auto_approve_escalate, needs_confirm
from agent.types import ToolMessage, UserMessage


@dataclass(frozen=True)
class ToolExecution:
    status: Literal['ok', 'failed', 'blocked', 'declined', 'invalid', 'error', 'mock']
    content: str
    gate: str = ''

    @property
    def success(self):
        return self.status in ('ok', 'mock')


def execute_tool(registry, action, *, approve=None, mode=None, mock=False):
    check = guardrail(action)
    if check.disposition == 'deny':
        return ToolExecution('blocked', check.reason)
    if check.disposition == 'escalate' and not auto_approve_escalate(mode):
        if approve is None or not approve(action, 'escalate', check.reason):
            return ToolExecution('declined', check.reason, 'escalate')
    if needs_confirm(mode, action.tool or ''):
        if approve is None or not approve(action, 'edit', '写入文件需要确认'):
            return ToolExecution('declined', (action.args or {}).get('path', ''), 'edit')
    if mock:
        return ToolExecution('mock', 'Mock 模式：未执行工具')
    try:
        result = registry.execute(action.tool or '', action.args or {})
    except ValueError as error:
        return ToolExecution('invalid', str(error))
    except Exception as error:
        return ToolExecution('error', str(error))
    return ToolExecution('ok', result.data) if result.success else ToolExecution('failed', result.error)


def record_tool_reply(messages, action, content):
    if action.tool_call_id:
        messages.append(ToolMessage(content=content, tool_call_id=action.tool_call_id))
    elif action.type != 'done':
        messages.append(UserMessage(content=content))
