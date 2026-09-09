"""Agent Loop — 核心 ReAct 循环：Context Assembly → LLM → Guardrail → Execute → Feedback。"""

import sys
from collections.abc import Callable
from agent.types import (
    Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage,
    Action, LLMResponse, BaseTool,
)
from agent.registry import ToolRegistry
from agent.guardrail import guardrail
from agent.memory import FileMemory
from agent.tracer import Tracer


def build_system_prompt(workspace: str) -> str:
    return f"""你是 coding-agent-harness，一个编程智能体。
你有以下工具可用：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- shell: 执行 Shell 命令

你的工作流程：
1. 读取任务描述文件，理解需要实现什么
2. 编写解决方案代码
3. 编写测试代码覆盖验收用例
4. 运行测试，根据测试结果迭代修改代码
5. 所有测试通过后，调用 done(answer) 完成任务

文件存放规范：
- 根据需要创建合适的文件夹和文件组织代码
- 不要把所有文件都放在根目录下

重要约束：
- 使用通用的算法实现，不得针对特定输入硬编码返回值
- 每个任务独立完成，不要在前一个任务的代码基础上修改
- 如果 shell 命令被拦截，尝试用其他方法实现

当前工作目录：{workspace}"""


def build_goal_prompt(task_name: str, task_desc: str) -> str:
    return f"""请完成以下编程任务：

{task_name}

任务描述：
{task_desc}

请按照工作流程完成此任务。记住：使用通用算法，不要硬编码测试用例的返回值。"""


def _truncate(text: str, max_len: int = 120) -> str:
    """截断过长的文本用于终端展示。"""
    if len(text) <= max_len:
        return text
    return text[:max_len] + '...'


def _print_tool_call(action: Action):
    """紧凑展示工具调用，类似 Claude Code 风格。"""
    tool = action.tool or ''
    args = action.args or {}
    if tool == 'read_file':
        print(f"  → read_file(\"{args.get('path', '')}\")")
    elif tool == 'write_file':
        path = args.get('path', '')
        print(f"  → write_file(\"{path}\")")
    elif tool == 'shell':
        cmd = args.get('command', '')
        print(f"  → shell(\"{_truncate(cmd, 100)}\")")
    else:
        print(f"  → {tool}({_truncate(str(args))})")


def _print_result(tool_name: str, success: bool, result_text: str):
    """紧凑展示工具结果。"""
    prefix = '✓' if success else '✗'
    text = _truncate(result_text, 200).replace('\n', ' ')
    print(f"  {prefix} {text}")


def run_agent(
    goal: str,
    task_name: str,
    llm,
    tool_registry: ToolRegistry,
    memory: FileMemory,
    tracer: Tracer,
    max_steps: int = 30,
    approver: Callable[[Action], bool] | None = None,
    workspace: str = '.',
) -> str:
    """运行 Agent Loop，返回完成消息。"""

    messages: list[Message] = [
        SystemMessage(content=build_system_prompt(workspace)),
        UserMessage(content=build_goal_prompt(task_name, goal)),
    ]

    done = False
    answer = ''
    steps = 0
    tools: list[BaseTool] = tool_registry.list()

    print()  # 空行分隔

    while not done and steps < max_steps:
        steps += 1

        # 1. 调用 LLM
        try:
            response: LLMResponse = llm.chat(messages, tools)
        except Exception as e:
            error_msg = f"LLM 调用失败: {e}"
            print(f"  ⚠ {error_msg}")
            messages.append(UserMessage(content=error_msg))
            tracer.record(steps, Action(type='call_tool'), result=error_msg)
            continue

        # 追加 assistant message
        if response.message:
            messages.append(response.message)
            if response.message.content:
                text = response.message.content.strip()
                if text:
                    print(text)

        # 2. 解析 Action
        action = response.action
        if action is None:
            continue

        # 3. Guardrail 检查
        guard = guardrail(action)
        if guard.disposition == 'deny':
            msg = f"安全护栏: {guard.reason}"
            print(f"  ⛔ DENY: {guard.reason}")
            messages.append(UserMessage(content=msg))
            tracer.record(steps, action, result=f'[DENIED] {guard.reason}', feedback=msg)
            continue

        if guard.disposition == 'escalate':
            if approver:
                approved = approver(action)
            else:
                approved = False

            if not approved:
                msg = f"{guard.reason}"
                print(f"  ⛔ ESCALATE DENIED: {guard.reason}")
                messages.append(UserMessage(content=msg))
                tracer.record(steps, action, result=f'[ESCALATED-DENIED] {guard.reason}')
                continue
            print(f"  ⚡ ESCALATE APPROVED: {guard.reason}")

        # 4. 分发执行
        if action.type == 'done':
            answer = action.answer or 'Task completed'
            done = True
            print(f"  🎯 {answer}")
            tracer.record(steps, action, result=answer)
            break

        elif action.type == 'take_note':
            key = action.note_key or ''
            value = action.note_value or ''
            if key:
                memory.write(key, value)
            result_text = f'已记录笔记: {key}={value[:50] if value else ""}'
            print(f"  📝 {key} = {_truncate(value or '')}")
            if action.tool_call_id:
                messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
            else:
                messages.append(UserMessage(content=result_text))
            tracer.record(steps, action, result=result_text)
            continue

        elif action.type == 'call_tool':
            tool_name = action.tool or ''
            tool_args = action.args or {}

            # 打印工具调用
            _print_tool_call(action)

            try:
                tool_result = tool_registry.execute(tool_name, tool_args)
            except ValueError as e:
                result_text = f'错误: {e}'
                print(f"  ⚠ {result_text}")
                messages.append(UserMessage(content=result_text))
                tracer.record(steps, action, result=result_text)
                continue
            except Exception as e:
                result_text = f'工具执行异常: {e}'
                print(f"  ⚠ {result_text}")
                messages.append(UserMessage(content=result_text))
                tracer.record(steps, action, result=result_text)
                continue

            result_text = tool_result.data if tool_result.success else tool_result.error
            _print_result(tool_name, tool_result.success, result_text)

            # 5. Feedback Injection
            if action.tool_call_id:
                messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
            else:
                messages.append(UserMessage(content=result_text))

            feedback = None
            if not tool_result.success:
                if tool_name == 'shell' and '返回码' in (tool_result.error or ''):
                    feedback = f"命令执行失败（返回码非零），这是出错信息，请参考前面的执行结果修正你的方法后重试。"
                    messages.append(UserMessage(content=feedback))
                elif tool_name in ('read_file', 'write_file'):
                    feedback = f"文件操作失败，请检查路径是否正确后重试。"
                    messages.append(UserMessage(content=feedback))

            tracer.record(steps, action, result=result_text, feedback=feedback)
            continue

        else:
            msg = f'未知动作类型: {action.type}'
            print(f"  ⚠ {msg}")
            messages.append(UserMessage(content=msg))
            tracer.record(steps, action, result=msg)

    # 收尾
    memory.consolidate()
    tracer.flush()

    if not done:
        answer = f"达到最大步数 ({max_steps})，任务未完成。"
        print(f"\n  ⏹ {answer}")

    return answer