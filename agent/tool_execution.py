"""共享工具管线。确认的交互由调用入口提供。"""

from dataclasses import dataclass
from typing import Literal

from agent.guardrail import guardrail
from agent.events import RunCancelled
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
    if check.disposition != 'escalate' and needs_confirm(mode, action.tool or ''):
        shell = action.tool == 'shell'
        gate = 'shell' if shell else 'edit'
        reason = '运行 Shell 命令需要确认' if shell else '写入文件需要确认'
        if approve is None or not approve(action, gate, reason):
            return ToolExecution('declined', reason if shell else (action.args or {}).get('path', ''), gate)
    if mock:
        return ToolExecution('mock', 'Mock 模式：未执行工具')
    try:
        result = registry.execute(action.tool or '', action.args or {})
    except RunCancelled:
        raise
    except ValueError as error:
        return ToolExecution('invalid', str(error))
    except Exception as error:
        return ToolExecution('error', str(error))
    if result.success:
        return ToolExecution('ok', result.data)
    return ToolExecution('failed', '\n'.join(part for part in (result.error, result.data) if part))


def record_tool_reply(messages, action, content):
    if action.tool_call_id:
        messages.append(ToolMessage(content=content, tool_call_id=action.tool_call_id))
    elif action.type != 'done':
        messages.append(UserMessage(content=content, internal=True))
