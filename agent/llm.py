"""LLM Layer — 接口抽象、OpenAI/DeepSeek 实现、Mock LLM。"""

import json
import os
import time
from openai import OpenAI
from agent.types import (
    Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage,
    Action, ToolCall, LLMResponse, BaseTool,
)


# ---- Tool Definitions 构建（OpenAI 兼容格式）----

def build_tool_definitions(tools: list[BaseTool], include_done: bool = True,
                           include_take_note: bool = True) -> list[dict]:
    """将工具注册表合成为 OpenAI 兼容的 tool 定义。"""
    defs = []
    for t in tools:
        defs.append({
            'type': 'function',
            'function': {
                'name': t.name,
                'description': t.description,
                'parameters': t.parameters or {'type': 'object', 'properties': {}},
            },
        })
    if include_done:
        defs.append({
            'type': 'function',
            'function': {
                'name': 'done',
                'description': '完成任务并给出最终答案',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'answer': {'type': 'string', 'description': '最终答案说明'},
                    },
                    'required': ['answer'],
                },
            },
        })
    if include_take_note:
        defs.append({
            'type': 'function',
            'function': {
                'name': 'take_note',
                'description': '将一条键值对记录到记忆中，供后续会话回顾',
                'parameters': {
                    'type': 'object',
                    'properties': {
                        'key': {'type': 'string', 'description': '键'},
                        'value': {'type': 'string', 'description': '值'},
                    },
                    'required': ['key', 'value'],
                },
            },
        })
    return defs


# ---- Action 解析 ----

def parse_tool_call(tc: ToolCall) -> Action:
    """将 LLM 返回的 tool call 解析为 Action。"""
    name = tc.function_name
    try:
        args = json.loads(tc.function_arguments) if tc.function_arguments else {}
    except json.JSONDecodeError:
        args = {}

    if name == 'done':
        return Action(
            type='done',
            answer=args.get('answer', 'Task completed'),
            tool_call_id=tc.id,
        )
    elif name == 'take_note':
        return Action(
            type='take_note',
            note_key=args.get('key', ''),
            note_value=args.get('value', ''),
            tool_call_id=tc.id,
        )
    else:
        return Action(
            type='call_tool',
            tool=name,
            args=args,
            tool_call_id=tc.id,
        )


# ---- OpenAI Provider ----

class OpenAIProvider:
    """通过 OpenAI 兼容 API 调用 LLM（适用于 DeepSeek / OpenAI / OpenRouter 等）。"""

    MAX_RETRIES = 3  # 单次 chat 的最大尝试次数（含首次）

    def __init__(self, api_key: str, model: str = 'deepseek-chat',
                 base_url: str = 'https://api.deepseek.com'):
        # httpx 不支持 SOCKS 代理，自动降级为 HTTP_PROXY/HTTPS_PROXY
        for var in ('all_proxy', 'ALL_PROXY'):
            if var in os.environ and os.environ[var].startswith('socks'):
                del os.environ[var]

        self.model = model
        self.client = OpenAI(api_key=api_key, base_url=base_url)

    def _convert_messages(self, messages: list[Message]) -> list[dict]:
        result = []
        for m in messages:
            if isinstance(m, SystemMessage):
                result.append({'role': 'system', 'content': m.content})
            elif isinstance(m, UserMessage):
                result.append({'role': 'user', 'content': m.content})
            elif isinstance(m, AssistantMessage):
                d = {'role': 'assistant', 'content': m.content}
                if m.tool_calls:
                    d['tool_calls'] = [
                        _tc if isinstance(_tc, dict) else {
                            'id': _tc.id,
                            'type': 'function',
                            'function': {
                                'name': _tc.function_name,
                                'arguments': _tc.function_arguments,
                            },
                        }
                        for _tc in m.tool_calls
                    ]
                result.append(d)
            elif isinstance(m, ToolMessage):
                result.append({
                    'role': 'tool',
                    'content': m.content,
                    'tool_call_id': m.tool_call_id,
                })
        return result

    def chat(self, messages: list[Message], tools: list[BaseTool],
             include_done: bool = True, include_take_note: bool = True) -> LLMResponse:
        api_messages = self._convert_messages(messages)
        tool_defs = build_tool_definitions(tools, include_done=include_done,
                                           include_take_note=include_take_note)

        # 指数退避重试（1s / 2s），最多 3 次尝试，覆盖网络抖动与限流
        response = None
        for attempt in range(1, self.MAX_RETRIES + 1):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=api_messages,
                    tools=tool_defs,
                    tool_choice='auto',
                )
                break
            except Exception as e:
                if attempt >= self.MAX_RETRIES:
                    raise
                delay = 2 ** (attempt - 1)
                print(f'  ⚠ LLM 调用失败（第 {attempt}/{self.MAX_RETRIES} 次），{delay}s 后重试: {e}')
                time.sleep(delay)

        choice = response.choices[0]
        msg = choice.message

        if msg.tool_calls:
            # 解析全部 tool call（模型可能一次给出多个并行调用）
            actions: list[Action] = []
            tc_dicts = []
            for tc_data in msg.tool_calls:
                tc = ToolCall(
                    id=tc_data.id,
                    type='function',
                    function_name=tc_data.function.name,
                    function_arguments=tc_data.function.arguments,
                )
                actions.append(parse_tool_call(tc))
                tc_dicts.append({
                    'id': tc.id,
                    'type': 'function',
                    'function': {
                        'name': tc.function_name,
                        'arguments': tc.function_arguments,
                    },
                })
            assistant_msg = AssistantMessage(content=msg.content, tool_calls=tc_dicts)
            return LLMResponse(actions=actions, message=assistant_msg)
        else:
            # 纯文本回复
            assistant_msg = AssistantMessage(content=msg.content)
            return LLMResponse(message=assistant_msg)

    def build_tool_definitions(self, tools: list[BaseTool]) -> list[dict]:
        return build_tool_definitions(tools)


# ---- Mock LLM ----

class MockLLM:
    """Mock LLM — 返回预定义的 Action 序列，用于测试。"""

    def __init__(self, responses: list[Action | str]):
        """
        Args:
            responses: 预定义的响应序列。Action 表示 tool call，str 表示文本回复。
        """
        self.responses = list(responses)
        self.call_count = 0

    def chat(self, messages: list[Message], tools: list[BaseTool]) -> LLMResponse:
        if self.call_count >= len(self.responses):
            # 超出预设响应，返回 done
            action = Action(type='done', answer='Mock completed')
            return LLMResponse(
                actions=[action],
                message=AssistantMessage(content=None, tool_calls=[]),
            )

        r = self.responses[self.call_count]
        self.call_count += 1

        if isinstance(r, str):
            return LLMResponse(message=AssistantMessage(content=r))
        else:
            # Action
            return LLMResponse(
                actions=[r],
                message=AssistantMessage(
                    content=None,
                    tool_calls=[
                        {
                            # 与 Action.tool_call_id 保持一致，保证会话满足工具调用协议
                            'id': r.tool_call_id or f'mock_{self.call_count}',
                            'type': 'function',
                            'function': {
                                'name': r.tool or '',
                                'arguments': json.dumps(r.args or {}),
                            },
                        }
                    ],
                ),
            )