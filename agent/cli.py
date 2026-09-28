"""CLI 入口 — 命令行解析 + 交互式 REPL，支持斜杠命令和自然语言混合输入。"""

import argparse
import re
import sys
from pathlib import Path

from agent.config import load_config, save_config, ensure_config
from agent.core import run_agent, _print_tool_call, _print_result, build_memory_block
from agent.guardrail import guardrail
from agent.llm import OpenAIProvider, MockLLM
from agent.registry import create_default_registry
from agent.memory import FileMemory
from agent.tracer import Tracer
from agent.types import (
    Action, Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage, LLMResponse,
)

# ── 配置键映射（自然语言 → config.json 字段名） ──
KEY_MAP = {
    'key': 'api_key',
    'api_key': 'api_key',
    'api key': 'api_key',
    'api-key': 'api_key',
    'apikey': 'api_key',
    'model': 'model',
    'base': 'api_base',
    'base url': 'api_base',
    'base_url': 'api_base',
    'api_base': 'api_base',
    'api base': 'api_base',
    'api-base': 'api_base',
}

DISPLAY_NAMES = {
    'api_key': 'API Key',
    'model': 'Model',
    'api_base': 'Base URL',
}

# 命令行参数名 → config.json 字段名
ARG_KEY_MAP = {
    'LLM_API_KEY': 'api_key',
    'LLM_MODEL': 'model',
    'LLM_API_BASE': 'api_base',
}


# ── 自然语言解析 ──

def _extract_set_value(text: str) -> tuple[str | None, str | None]:
    """解析"设置 X 为/成/改成/改为 Y"或"X=Y"类语句，返回 (key, value)。"""
    key_patterns = '|'.join(re.escape(k) for k in KEY_MAP)

    m = re.search(
        rf'(?:设置|配置|把|将)\s*({key_patterns})\s*(?:为|成|改成|改为|设置成|设定为|=|:)\s*(.+)',
        text, re.IGNORECASE
    )
    if m:
        return m.group(1).strip(), m.group(2).strip()

    m = re.search(rf'({key_patterns})\s*[=:]\s*(.+)', text, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()

    return None, None


def _parse_intent(text: str) -> dict:
    """解析自然语言输入，返回意图字典。"""
    text = text.strip()

    # ── 斜杠命令 ──
    if text.startswith('/'):
        parts = text[1:].split(maxsplit=1)
        cmd = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ''

        if cmd in ('exit', 'quit'):
            return {'type': 'exit'}
        if cmd in ('help', '?'):
            return {'type': 'help'}
        if cmd == 'config':
            return {'type': 'show_config'}
        if cmd == 'model':
            val = arg.strip().lstrip('=').strip() if arg else ''
            return {'type': 'set', 'key': 'model', 'value': val} if val else {'type': 'show_config'}
        if cmd in ('key', 'apikey', 'api-key'):
            val = arg.strip().lstrip('=').strip() if arg else ''
            return {'type': 'set', 'key': 'api_key', 'value': val} if val else {'type': 'show_config'}
        if cmd in ('base-url', 'base', 'base_url'):
            val = arg.strip().lstrip('=').strip() if arg else ''
            return {'type': 'set', 'key': 'api_base', 'value': val} if val else {'type': 'show_config'}
        if cmd in ('run',):
            run_file = arg.strip()
            return {'type': 'run', 'file': run_file} if run_file else {'type': 'help'}

        return {'type': 'help'}

    # ── 自然语言 ──
    text_lower = text.lower()

    if any(kw in text_lower for kw in ('退出', 'exit', 'quit', 'bye', '再见', '结束')):
        return {'type': 'exit'}

    if any(kw in text_lower for kw in ('帮助', 'help', '怎么用', '？', '?')):
        return {'type': 'help'}

    if any(kw in text for kw in ('查看', '显示', '看看', '当前配置', 'show config', '/config')):
        return {'type': 'show_config'}

    raw_key, value = _extract_set_value(text)
    if raw_key and value:
        norm_key = KEY_MAP.get(raw_key.lower())
        if norm_key:
            return {'type': 'set', 'key': norm_key, 'value': value}

    # 其他所有输入 → 交给 LLM 处理
    return {'type': 'chat', 'text': text}


# ── REPL 循环 ──

def _mask_secret(secret: str) -> str:
    """脱敏展示密钥类配置。"""
    if not secret:
        return '(未设置)'
    if len(secret) <= 10:
        return '***'
    return secret[:6] + '*' * (len(secret) - 10) + secret[-4:]


def _show_config():
    """显示当前配置。"""
    cfg = load_config()
    masked = _mask_secret(cfg.get('api_key', ''))
    print()
    print(f'  API Key:   {masked}')
    print(f'  Model:     {cfg.get("model", "deepseek-chat")}')
    print(f'  Base URL:  {cfg.get("api_base", "https://api.deepseek.com")}')
    print(f'  Max Steps: {cfg.get("max_steps", 30)}')


def _show_help():
    """显示帮助信息。"""
    print()
    print('  /config          查看当前配置')
    print('  /model = <name>  设置模型')
    print('  /key = <key>     设置 API Key')
    print('  /base-url = <url> 设置 API 地址')
    print('  /run <file>      运行任务文件')
    print('  /help            显示帮助')
    print('  /exit            退出')
    print()
    print('  也可以直接输入自然语言，例如:')
    print('  "实现一个斐波那契数列"')
    print('  "读取当前目录的文件"')


def _chat_system_prompt(workspace: str) -> str:
    """对话模式的系统提示。"""
    return f"""你是一个 AI 助手，擅长编程和命令行操作。你可以与用户自由对话，也可以在需要时使用以下工具：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- shell: 执行 Shell 命令

如果用户只是打招呼或闲聊，正常回应即可，不用调用工具。
如果需要完成编程任务，先理解需求再使用合适的工具。

当前工作目录：{workspace}"""


def _console_approver(action: Action) -> bool:
    """batch 模式下 escalate 操作的人工确认。"""
    args = action.args or {}
    try:
        ans = input(f'  ⚡ 危险操作待人工确认: {action.tool}({args})。批准执行? [y/N] ')
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ('y', 'yes')


def _repl_handle_action(
    action: Action,
    messages: list,
    tool_registry,
    memory: FileMemory,
    mock: bool,
) -> str:
    """处理 repl 聊天循环中的单个动作：护栏检查 → 执行 → 结果回灌。

    返回用于 tracer 记录的结果摘要。
    """
    if action.type == 'done':
        answer = action.answer or '完成'
        print(f'  🎯 {answer}')
        return answer

    if action.type == 'take_note':
        key = action.note_key or ''
        value = action.note_value or ''
        if key:
            memory.write(key, value)
            memory.consolidate()
        result_text = f'已记录: {key}={value[:50] if value else ""}'
        print(f'  📝 {key} = {value[:80]}')
        if action.tool_call_id:
            messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
        else:
            messages.append(UserMessage(content=result_text))
        return result_text

    if action.type != 'call_tool':
        msg = f'未知动作类型: {action.type}'
        print(f'  ⚠ {msg}')
        messages.append(UserMessage(content=msg))
        return msg

    t_name = action.tool or ''
    t_args = action.args or {}
    _print_tool_call(action)

    if mock:
        print('  ✓ (mock)')
        if action.tool_call_id:
            messages.append(ToolMessage(content='(mock)', tool_call_id=action.tool_call_id))
        return '(mock)'

    # 与 batch 模式一致：工具执行前必须过安全护栏
    guard_result = guardrail(action)
    if guard_result.disposition == 'deny':
        msg = f'安全护栏: {guard_result.reason}'
        print(f'  ⛔ DENY: {guard_result.reason}')
        if action.tool_call_id:
            messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
        else:
            messages.append(UserMessage(content=msg))
        return f'[DENIED] {guard_result.reason}'

    if guard_result.disposition == 'escalate':
        try:
            answer = input(f'  ⚡ {guard_result.reason}，需要人工确认。批准执行? [y/N] ')
            approved = answer.strip().lower() in ('y', 'yes')
        except (EOFError, KeyboardInterrupt):
            approved = False
            print()
        if not approved:
            msg = f'操作被拒绝: {guard_result.reason}'
            print('  ⛔ ESCALATE DENIED')
            if action.tool_call_id:
                messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
            else:
                messages.append(UserMessage(content=msg))
            return f'[ESCALATED-DENIED] {guard_result.reason}'
        print('  ⚡ ESCALATE APPROVED')

    try:
        tr = tool_registry.execute(t_name, t_args)
    except Exception as e:
        print(f'  ⚠ {e}')
        if action.tool_call_id:
            messages.append(ToolMessage(content=str(e), tool_call_id=action.tool_call_id))
        return f'[EXC] {e}'

    result_text = tr.data if tr.success else tr.error
    _print_result(t_name, tr.success, result_text)
    if action.tool_call_id:
        messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
    else:
        messages.append(UserMessage(content=result_text))
    return result_text


def repl(mock: bool = False, max_steps: int = 30):
    """交互式 REPL 主循环 — 持续对话模式。"""
    ensure_config()
    config = load_config()
    memory = FileMemory(config['memory_path'])
    tracer = Tracer(config['traces_dir'])

    # 初始化 LLM 和工具（对话期间复用）
    if mock:
        llm = MockLLM(responses=[])
        tools = None
    else:
        if not config['api_key']:
            print('[!] 错误：未设置 API Key。')
            print('   请先配置: /key = sk-xxx')
            return
        llm = OpenAIProvider(
            api_key=config['api_key'],
            model=config['model'],
            base_url=config['api_base'],
        )
        tool_registry = create_default_registry(shell_timeout=config['shell_timeout'])
        tools = tool_registry.list()

    messages: list[Message] = []  # 持续对话的消息历史

    print('Coding Agent 交互模式（输入 /help 查看命令）')

    try:
        while True:
            try:
                text = input('\n> ').strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break

            if not text:
                continue

            intent = _parse_intent(text)

            if intent['type'] == 'exit':
                print('  再见！')
                break

            elif intent['type'] == 'help':
                _show_help()

            elif intent['type'] == 'show_config':
                _show_config()

            elif intent['type'] == 'set':
                cfg_key = intent['key']
                cfg_value = intent['value']
                save_config({cfg_key: cfg_value})
                # 同步更新内存中的 config，否则本次会话重建 Provider 时仍用旧配置
                config[cfg_key] = cfg_value
                # 如果 LLM 已初始化，动态更新配置
                if not mock:
                    llm = OpenAIProvider(
                        api_key=config['api_key'],
                        model=config['model'],
                        base_url=config['api_base'],
                    )
                display = DISPLAY_NAMES.get(cfg_key, cfg_key)
                shown = _mask_secret(cfg_value) if cfg_key == 'api_key' else cfg_value
                print(f'  ✓ 已更新 {display} = {shown}')

            elif intent['type'] in ('chat', 'run'):
                # 首次对话时初始化消息
                if not messages:
                    workspace = config['workspace']
                    system_prompt = _chat_system_prompt(workspace) + build_memory_block(memory)
                    messages.append(SystemMessage(content=system_prompt))

                if intent['type'] == 'run':
                    # /run 读取任务文件内容交给 LLM，而不是把路径字符串发过去
                    run_file = Path(intent.get('file', ''))
                    if not run_file.exists():
                        print(f'  ⚠ 任务文件不存在: {run_file}')
                        continue
                    chat_text = run_file.read_text(encoding='utf-8')
                else:
                    chat_text = intent.get('text', '')
                messages.append(UserMessage(content=chat_text))

                # 对话循环：LLM → 工具调用 → LLM → ... → 文本回复
                step = 0
                while True:
                    try:
                        response: LLMResponse = llm.chat(messages, tools or [])
                    except Exception as e:
                        print(f'  ⚠ LLM 调用失败: {e}')
                        break

                    if response.message:
                        messages.append(response.message)
                        if response.message.content:
                            print(f'\n{response.message.content}\n')

                    if not response.actions:
                        break

                    # 依次处理本轮全部动作（可能包含并行 tool calls）
                    any_done = False
                    for action in response.actions:
                        step += 1
                        result_summary = _repl_handle_action(
                            action, messages,
                            tool_registry if not mock else None,
                            memory, mock,
                        )
                        tracer.record(step, action, result=result_summary)
                        if action.type == 'done':
                            any_done = True
                            break
                    if any_done:
                        break
    finally:
        tracer.flush()

    print()


# ── 参数解析 ──

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='freud',
        description='Freud — AI 编程助手',
        epilog='无参数启动进入交互模式 | 示例: freud --file task.md',
    )
    parser.add_argument(
        '--mock',
        action='store_true',
        help='使用 mock LLM（无需 API key，用于测试）',
    )
    parser.add_argument(
        '--file',
        type=str,
        help='任务描述文件（markdown），指定后直接运行，不进入交互模式',
    )
    parser.add_argument(
        '--name',
        type=str,
        default='自定义任务',
        help='任务名称（默认"自定义任务"）',
    )
    parser.add_argument(
        '--max-steps',
        type=int,
        default=30,
        help='最大迭代步数（默认 30）',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='自动批准 escalate 级危险操作（无人值守模式，慎用）',
    )
    parser.add_argument(
        '-v', '--version',
        action='version',
        version='freud 0.1.0',
    )
    return parser


def run(args: argparse.Namespace) -> str:
    """执行 Agent（batch 模式）。"""
    config = load_config()

    if args.mock:
        llm = MockLLM(responses=[])
    else:
        api_key = config['api_key']
        if not api_key:
            print('错误：未设置 API Key')
            sys.exit(1)

        llm = OpenAIProvider(
            api_key=api_key,
            model=config['model'],
            base_url=config['api_base'],
        )

    try:
        goal = Path(args.file).read_text(encoding='utf-8')
    except FileNotFoundError:
        print(f'文件未找到: {args.file}')
        sys.exit(1)

    if not goal.strip():
        print('任务描述为空')
        sys.exit(1)

    tool_registry = create_default_registry(shell_timeout=config['shell_timeout'])
    memory = FileMemory(config['memory_path'])
    tracer = Tracer(config['traces_dir'])
    workspace = config['workspace']

    # escalate 审批策略：--yes 全自动批准；交互终端人工确认；非交互默认拒绝
    if args.yes:
        approver = lambda action: True
    elif sys.stdin.isatty():
        approver = _console_approver
    else:
        approver = None

    answer = run_agent(
        goal=goal,
        task_name=args.name,
        llm=llm,
        tool_registry=tool_registry,
        memory=memory,
        tracer=tracer,
        max_steps=args.max_steps,
        approver=approver,
        workspace=workspace,
    )
    return answer


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.file:
        repl(mock=args.mock, max_steps=args.max_steps)
        return

    answer = run(args)
    print(f'\n结果: {answer}')


if __name__ == '__main__':
    main()
