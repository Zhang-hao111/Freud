"""Agent Loop — 核心 ReAct 循环：Context Assembly → LLM → Guardrail → Execute → Feedback。"""

import sys
from collections.abc import Callable
from agent.types import (
    Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage,
    Action, LLMResponse, BaseTool,
)
from agent.registry import ToolRegistry
from agent.guardrail import guardrail
from agent.permissions import MODES, auto_approve_escalate, needs_confirm
from agent.memory import FileMemory
from agent.tracer import Tracer
from agent import ui


def build_system_prompt(workspace: str) -> str:
    return f"""你是 coding-agent-harness，一个编程智能体。
你有以下工具可用：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- edit_file: 对已有文件做精确字符串替换，改动局部时优先用它
- grep: 用正则搜索文件内容
- glob: 按 glob 模式查找文件路径
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


def build_memory_block(memory: FileMemory | None) -> str:
    """把已有记忆注入系统提示，形成 take_note 写入 → 上下文检索的闭环。"""
    if memory is None:
        return ''
    entries = [e for e in memory.items() if e.key]
    if not entries:
        return ''
    lines = []
    for e in entries[:20]:
        value = (e.value or '').strip()
        if len(value) > 300:
            value = value[:300] + '...'
        lines.append(f'- {e.key}：{value}')
    return '\n\n已记录的记忆（来自此前会话的 take_note 笔记，供参考）：\n' + '\n'.join(lines)


def build_goal_prompt(task_name: str, task_desc: str) -> str:
    return f"""请完成以下编程任务：

{task_name}

任务描述：
{task_desc}

请按照工作流程完成此任务。记住：使用通用算法，不要硬编码测试用例的返回值。"""


def _print_tool_call(action: Action):
    """⏺ 工具调用行（Claude Code 风格）。"""
    print(ui.render_tool_line(action.tool or '', action.args or {}))


def _print_result(tool_name: str, success: bool, result_text: str):
    """⎿ 结果预览行。"""
    print(ui.render_result_line(result_text, success))


def run_agent(
    goal: str,
    task_name: str,
    llm,
    tool_registry: ToolRegistry,
    memory: FileMemory,
    tracer: Tracer,
    max_steps: int = 30,
    approver: Callable[[Action], bool] | None = None,
    confirmer: Callable[[Action], bool] | None = None,
    mode: str = 'ask',
    workspace: str = '.',
) -> str:
    """运行 Agent Loop，返回完成消息。

    mode 为三档权限模式（见 agent.permissions），与 REPL 共用同一套判定：
    ask 下文件改动需经 confirmer 确认（无 confirmer 可问则直接拒绝），
    yolo 自动批准 escalate 与文件改动。approver 仅在 mode 非 yolo 时
    用于 escalate 级命令的人工确认。
    """
    if mode not in MODES:
        raise ValueError(f"未知权限模式: {mode}，可选 {'/'.join(MODES)}")

    messages: list[Message] = [
        SystemMessage(content=build_system_prompt(workspace) + build_memory_block(memory)),
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
            print(f"  {ui.paint('yellow', '⚠')} {error_msg}")
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

        # 2. 依次处理本轮全部动作（模型可能一次给出多个并行 tool call）
        for action in response.actions:
            if done:
                break

            # 3. Guardrail 检查
            guard = guardrail(action)
            if guard.disposition == 'deny':
                msg = f"⛔ 安全护栏: {guard.reason}"
                print(ui.render_tool_line(action.tool or '', action.args or {}))
                print(ui.render_result_line(msg, ok=False))
                # 拦截也必须以 ToolMessage 响应 tool_call，否则会话违反协议、下一轮 API 400
                if action.tool_call_id:
                    messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
                else:
                    messages.append(UserMessage(content=msg))
                tracer.record(steps, action, result=f'[DENIED] {guard.reason}', feedback=msg)
                continue

            if guard.disposition == 'escalate':
                if auto_approve_escalate(mode):
                    approved = True
                    print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', 'yolo 模式自动批准')}")
                elif approver:
                    approved = approver(action)
                else:
                    approved = False

                if not approved:
                    msg = f"操作被拒绝: {guard.reason}"
                    print(ui.render_tool_line(action.tool or '', action.args or {}))
                    print(ui.render_result_line(msg, ok=False))
                    if action.tool_call_id:
                        messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
                    else:
                        messages.append(UserMessage(content=msg))
                    tracer.record(steps, action, result=f'[ESCALATED-DENIED] {guard.reason}')
                    continue
                if not auto_approve_escalate(mode):
                    print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', '已人工批准')}")

            # ask 模式：文件改动执行前必须确认（REPL 传 confirmer；非交互 batch 无 confirmer 则拒绝）
            if needs_confirm(mode, action.tool or ''):
                target = (action.args or {}).get('path', '')
                if confirmer:
                    if not confirmer(action):
                        msg = f'用户拒绝本次文件修改: {target}'
                        print(ui.render_result_line(msg, ok=False))
                        if action.tool_call_id:
                            messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
                        else:
                            messages.append(UserMessage(content=msg))
                        tracer.record(steps, action, result=f'[USER-DENIED] {target}', feedback=msg)
                        continue
                else:
                    msg = ('ask 模式下文件修改需人工确认，当前无法询问；'
                           '无人值守请使用 --yes（等价 yolo）或 --mode accept/yolo')
                    print(ui.render_tool_line(action.tool or '', action.args or {}))
                    print(ui.render_result_line(msg, ok=False))
                    if action.tool_call_id:
                        messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
                    else:
                        messages.append(UserMessage(content=msg))
                    tracer.record(steps, action, result='[CONFIRM-UNAVAILABLE]', feedback=msg)
                    continue

            # 4. 分发执行
            if action.type == 'done':
                answer = action.answer or 'Task completed'
                done = True
                print(f"{ui.dot()} {ui.paint('bold', 'Done')}")
                for line in answer.splitlines() or ['(无答案)']:
                    print(f"  {line}")
                # done 也是一次 tool call，回灌 ToolMessage 保持协议完整
                if action.tool_call_id:
                    messages.append(ToolMessage(content=answer, tool_call_id=action.tool_call_id))
                tracer.record(steps, action, result=answer)
                break

            elif action.type == 'take_note':
                key = action.note_key or ''
                value = action.note_value or ''
                if key:
                    memory.write(key, value)
                result_text = f'已记录笔记: {key}={value[:50] if value else ""}'
                print(f"{ui.dot()} {ui.paint('bold', 'Note')}({key})")
                print(ui.render_result_line(value or '(空)', True))
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
                    print(f"  {ui.paint('yellow', '⚠')} {result_text}")
                    if action.tool_call_id:
                        messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
                    else:
                        messages.append(UserMessage(content=result_text))
                    tracer.record(steps, action, result=result_text)
                    continue
                except Exception as e:
                    result_text = f'工具执行异常: {e}'
                    print(f"  {ui.paint('yellow', '⚠')} {result_text}")
                    if action.tool_call_id:
                        messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
                    else:
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
                    elif tool_name in ('read_file', 'write_file', 'edit_file'):
                        feedback = f"文件操作失败，请检查路径是否正确后重试。"
                        messages.append(UserMessage(content=feedback))

                tracer.record(steps, action, result=result_text, feedback=feedback)
                continue

            else:
                msg = f'未知动作类型: {action.type}'
                print(f"  {ui.paint('yellow', '⚠')} {msg}")
                messages.append(UserMessage(content=msg))
                tracer.record(steps, action, result=msg)

    # 收尾
    memory.consolidate()
    tracer.flush()

    if not done:
        answer = f"达到最大步数 ({max_steps})，任务未完成。"
        print()
        print(ui.render_result_line(answer, ok=False))

    return answer
