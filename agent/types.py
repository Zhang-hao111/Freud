"""共享类型定义 — Action、Message、Tool 契约等。"""

from dataclasses import dataclass, field
from typing import Literal


# ---- Action ----

ActionType = Literal['call_tool', 'done', 'take_note']


@dataclass
class Action:
    """LLM 返回的结构化动作。"""
    type: ActionType
    tool: str | None = None
    args: dict | None = None
    answer: str | None = None
    note_key: str | None = None
    note_value: str | None = None
    tool_call_id: str | None = None


# ---- Message ----

@dataclass
class SystemMessage:
    role: Literal['system'] = 'system'
    content: str = ''


@dataclass
class UserMessage:
    role: Literal['user'] = 'user'
    content: str = ''


@dataclass
class AssistantMessage:
    role: Literal['assistant'] = 'assistant'
    content: str | None = None
    tool_calls: list | None = None  # list[ToolCall]


@dataclass
class ToolMessage:
    role: Literal['tool'] = 'tool'
    content: str = ''
    tool_call_id: str = ''


Message = SystemMessage | UserMessage | AssistantMessage | ToolMessage


@dataclass
class ToolCall:
    """OpenAI 格式的 tool call。"""
    id: str
    type: str = 'function'
    function_name: str = ''
    function_arguments: str = ''  # JSON string


# ---- Tool ----

@dataclass
class ToolResult:
    success: bool
    data: str = ''
    error: str = ''


class BaseTool:
    """工具基类 — 所有工具实现此接口。"""
    name: str = ''
    description: str = ''
    parameters: dict = field(default_factory=dict)

    def execute(self, args: dict) -> ToolResult:
        raise NotImplementedError


# ---- Guardrail ----

Disposition = Literal['allow', 'deny', 'escalate']


@dataclass
class GuardrailResult:
    disposition: Disposition = 'allow'
    reason: str = ''


@dataclass
class DangerousPattern:
    pattern: str  # regex string
    disposition: Disposition
    reason: str


# ---- Trace ----

@dataclass
class TraceEntry:
    step: int
    action: Action
    result: str
    timestamp: str
    feedback: str | None = None


# ---- Memory ----

@dataclass
class MemoryEntry:
    key: str
    value: str
    created_at: str
    updated_at: str


# ---- LLM ----

@dataclass
class LLMResponse:
    """一次 LLM 调用的结果：可能携带零个、一个或多个动作（并行 tool calls）。"""
    actions: list[Action] = field(default_factory=list)
    message: Message | None = None

    @property
    def action(self) -> Action | None:
        """兼容单动作访问：返回第一个动作。"""
        return self.actions[0] if self.actions else None