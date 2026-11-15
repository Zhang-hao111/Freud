"""网页流式事件协议。每个事件序列化为一行 NDJSON。"""

from typing import Callable, Literal, NotRequired, TypedDict


class TextEvent(TypedDict):
    type: Literal['progress', 'result', 'error', 'assistant_update']
    content: str


class AssistantEvent(TypedDict):
    type: Literal['assistant']
    content: str
    workflow_result: NotRequired[bool]


class ToolEvent(TypedDict):
    type: Literal['tool']
    tool: str
    args: dict


class ApprovalEvent(TypedDict):
    type: Literal['approval']
    id: str
    tool: str
    args: dict
    reason: str


Event = TextEvent | AssistantEvent | ToolEvent | ApprovalEvent
Emit = Callable[[Event], None]
