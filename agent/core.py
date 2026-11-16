"""Agent Loop — 核心 ReAct 循环：Context Assembly → LLM → Guardrail → Execute → Feedback。"""

from collections.abc import Callable
from agent.context import model_context
from agent.types import (
    AssistantMessage, SystemMessage, UserMessage, ToolMessage,
    Action,
)
from agent.registry import ToolRegistry
from agent.tool_execution import execute_tool, record_tool_reply
from agent.memory import FileMemory
from agent.tracer import Tracer
from agent import ui


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
- 遵循工具审批和工作区限制。遇到拒绝时说明原因，不绕过限制。

当前工作目录：{workspace}"""


def build_chat_system_prompt(workspace: str) -> str:
    """对话模式的系统提示。"""
    return f"""你是一个 AI 助手，擅长编程和命令行操作。你可以与用户自由对话，也可以在需要时使用以下工具：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- shell: 执行 Shell 命令

如果用户只是打招呼或闲聊，正常回应即可，不用调用工具。
如果需要完成编程任务，先理解需求再使用合适的工具。

当前工作目录：{workspace}"""


def run_chat_turn(messages, llm, tools, handle_action, on_text, *, max_steps=None, after_step=None,
                  stop_on_text=True, on_model_error=None, memory=None, checkpoint=None):
    """驱动一轮对话。工具执行与展示交给入口，所有工具调用得到对应回执。"""
    step = 0
    while max_steps is None or step < max_steps:
        if checkpoint:
            checkpoint()
        step += 1
        try:
            response = llm.chat(model_context(messages, memory), tools)
        except Exception as error:
            if on_model_error is None:
                raise
            on_model_error(step, error)
            return False
        message = response.message
        index = len(messages)
        if message:
            messages.append(message)
        done = False
        try:
            if checkpoint:
                checkpoint()
            if message and message.content:
                on_text(message.content)
            for action in response.actions:
                if checkpoint:
                    checkpoint()
                if action.type == 'invalid':
                    record_tool_reply(messages, action, action.error or '无效的工具调用')
                    continue
                handle_action(step, action)
                if action.type == 'done':
                    done = True
                    break
        finally:
            if message and message.tool_calls:
                tail = messages[index + 1:]
                answered = {item.tool_call_id for item in tail if isinstance(item, ToolMessage)}
                insert_at = index + 1
                for item in tail:
                    if not isinstance(item, ToolMessage):
                        break
                    insert_at += 1
                for call in message.tool_calls:
                    call_id = call['id'] if isinstance(call, dict) else call.id
                    if call_id not in answered:
                        messages.insert(insert_at, ToolMessage(content='操作中断，未完成。', tool_call_id=call_id))
                        insert_at += 1
        if response.actions and after_step:
            after_step()
        if done or (stop_on_text and not response.actions):
            return True
    return False


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
    workspace: str = '.',
    *,
    tool_executor=execute_tool,
    chat_loop=run_chat_turn,
    messages=None,
) -> str:
    """运行 Agent Loop，返回完成消息。"""

    messages = [] if messages is None else messages
    messages.extend([
        SystemMessage(content=build_system_prompt(workspace)),
        UserMessage(content=build_goal_prompt(task_name, goal)),
    ])

    answer = ''
    pending_feedback = []

    def approve(action, _gate, _reason):
        if approver and approver(action):
            print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', '已人工批准')}")
            return True
        return False

    def handle_action(step, action):
        nonlocal answer
        feedback = None
        if action.type == 'done':
            answer = action.answer or 'Task completed'
            print(f"{ui.dot()} {ui.paint('bold', 'Done')}")
            for line in answer.splitlines() or ['(无答案)']:
                print(f"  {line}")
            result = summary = answer
        elif action.type == 'take_note':
            key, value = action.note_key or '', action.note_value or ''
            if key:
                memory.write(key, value)
            result = summary = f'已记录笔记: {key}={value[:50]}'
            print(f"{ui.dot()} {ui.paint('bold', 'Note')}({key})")
            print(ui.render_result_line(value or '(空)', True))
        elif action.type == 'call_tool':
            _print_tool_call(action)
            outcome = tool_executor(tool_registry, action, approve=approve)
            result = summary = outcome.content
            if outcome.status == 'blocked':
                result = f'⛔ 安全护栏: {outcome.content}'
                summary = f'[DENIED] {outcome.content}'
                feedback = result
            elif outcome.status == 'declined':
                result = f'操作被拒绝: {outcome.content}'
                summary = f'[ESCALATED-DENIED] {outcome.content}'
            elif outcome.status in ('invalid', 'error'):
                prefix = '错误' if outcome.status == 'invalid' else '工具执行异常'
                result = summary = f'{prefix}: {outcome.content}'
            _print_result(action.tool or '', outcome.success, result)
            if outcome.status == 'failed':
                if action.tool == 'shell' and '返回码' in outcome.content:
                    feedback = '命令执行失败（返回码非零），请参考前面的执行结果修正方法后重试。'
                elif action.tool in ('read_file', 'write_file'):
                    feedback = '文件操作失败，请检查路径是否正确后重试。'
                if feedback:
                    pending_feedback.append(feedback)
        else:
            result = summary = f'未知动作类型: {action.type}'
            print(f"  {ui.paint('yellow', '⚠')} {result}")
        record_tool_reply(messages, action, result)
        tracer.record(step, action, result=summary, feedback=feedback)

    def on_model_error(step, error):
        nonlocal answer
        answer = f'LLM 调用失败: {error}'
        messages.append(AssistantMessage(content=answer))
        tracer.record(step, Action(type='call_tool'), result=answer)

    def flush_feedback():
        messages.extend(UserMessage(content=note, internal=True) for note in pending_feedback)
        pending_feedback.clear()

    print()

    def on_text(text):
        if text.strip():
            print(text.strip())

    done = chat_loop(messages, llm, tool_registry.list(), handle_action, on_text, max_steps=max_steps,
                     stop_on_text=False, on_model_error=on_model_error, after_step=flush_feedback, memory=memory)
    memory.consolidate()
    tracer.flush()
    if not done:
        answer = answer or f"达到最大步数 ({max_steps})，任务未完成。"
        print()
        print(ui.render_result_line(answer, ok=False))
    return answer
