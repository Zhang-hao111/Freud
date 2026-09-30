"""CLI 入口 — 命令行解析 + 交互式 REPL，支持斜杠命令和自然语言混合输入。"""

import argparse
import re
import shutil
import sys
from pathlib import Path

from agent.config import load_config, save_config, ensure_config
from agent.core import run_agent, _print_tool_call, _print_result, build_memory_block
from agent.guardrail import guardrail
from agent import term, ui
from agent.llm import OpenAIProvider, MockLLM
from agent.permissions import auto_approve_escalate, chip, cycle, needs_confirm
from agent.registry import create_default_registry


def _attach_governance_tools(tool_registry):
    """Hadoop 环境可用时追加 MovieLens 治理工具；环境缺失则跳过，主 agent 不受影响。"""
    try:
        from governance.jobs import DEFAULT_OUTPUT, DEFAULT_SOURCE
        from governance.tools import register_governance_tools
        registered = register_governance_tools(tool_registry, DEFAULT_SOURCE, DEFAULT_OUTPUT)
        if registered:
            print(f'[+] 治理工具已注册: {", ".join(registered)}')
    except Exception as error:
        print(f'[!] 治理工具未启用: {error}')
from agent.session import DEFAULT_TITLE, Session, SessionStore, SessionMemory, time_ago
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
        if cmd in ('resume', 'sessions'):
            return {'type': 'resume'}

        return {'type': 'help'}

    # ── 自然语言 ──
    text_lower = text.lower()

    if any(kw in text_lower for kw in ('退出', 'exit', 'quit', 'bye', '再见', '结束')):
        return {'type': 'exit'}

    if any(kw in text_lower for kw in ('帮助', 'help', '怎么用', '？', '?')):
        return {'type': 'help'}

    if any(kw in text_lower for kw in ('恢复会话', '找回会话', '继续上次', 'resume')):
        return {'type': 'resume'}

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


FREUD_VERSION = '0.1.0'

# Ψ（Psi）— 心理学符号，Freud 的像素吉祥物（小号）
_FREUD_LOGO = (
    '██ ██ ██',
    '██ ██ ██',
    '█████████',
    '   ███',
    '   ██',
)


def _print_banner(mock: bool = False, config: dict | None = None):
    """Claude Code 风格启动页：左侧吉祥物，右侧名称/版本/模型/目录。"""
    config = config or load_config()
    status = config.get('model', 'deepseek-chat')
    if mock:
        status = 'mock-llm'
    texts = [
        ui.paint('bold', 'Freud') + ui.paint('dim', f'  v{FREUD_VERSION}'),
        ui.paint('dim', status),
        ui.paint('dim', config.get('workspace', '.')),
    ]
    if mock:
        texts.append(ui.paint('yellow', 'Mock 模式：预定义响应，仅用于流程测试'))

    logo_width = max(ui.visual_width(row) for row in _FREUD_LOGO)
    print()
    for i in range(max(len(_FREUD_LOGO), len(texts))):
        art = _FREUD_LOGO[i] if i < len(_FREUD_LOGO) else ''
        txt = texts[i] if i < len(texts) else ''
        print('  ' + ui.paint_accent(art) + ' ' * (logo_width - ui.visual_width(art) + 3) + txt)
    print()


def _show_config():
    """显示当前配置。"""
    cfg = load_config()
    masked = _mask_secret(cfg.get('api_key', ''))
    print()
    print(f"  {ui.paint('dim', 'API Key:  ')} {masked}")
    print(f"  {ui.paint('dim', 'Model:    ')} {cfg.get('model', 'deepseek-chat')}")
    print(f"  {ui.paint('dim', 'Base URL: ')} {cfg.get('api_base', 'https://api.deepseek.com')}")
    print(f"  {ui.paint('dim', 'Max Steps:')} {cfg.get('max_steps', 30)}")


def _show_help():
    """显示帮助信息。"""
    rows = [
        ('/config', '查看当前配置'),
        ('/model = <name>', '设置模型'),
        ('/key = <key>', '设置 API Key'),
        ('/base-url = <url>', '设置 API 地址'),
        ('/run <file>', '运行任务文件'),
        ('/resume', '恢复历史会话（记忆与上下文）'),
        ('/help', '显示帮助'),
        ('/exit', '退出'),
    ]
    width = max(ui.visual_width(cmd) for cmd, _ in rows)
    print()
    print(ui.paint('bold', '  命令'))
    for cmd, desc in rows:
        print(f"  {ui.paint('cyan', ui.pad(cmd, width))}  {ui.paint('dim', desc)}")
    print()
    print(ui.paint('bold', '  也可以直接输入自然语言，例如'))
    print(ui.paint('dim', '  "实现一个斐波那契数列"'))
    print(ui.paint('dim', '  "读取当前目录的文件"'))
    print()


def _chat_system_prompt(workspace: str) -> str:
    """对话模式的系统提示。"""
    return f"""你是一个 AI 助手，擅长编程和命令行操作。你可以与用户自由对话，也可以在需要时使用以下工具：
- read_file: 读取文件内容
- write_file: 写入文件内容（自动创建父目录）
- edit_file: 对已有文件做精确字符串替换，改动局部时优先用它
- grep: 用正则搜索文件内容
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
    memory: SessionMemory,
    mock: bool,
    state: dict | None = None,
) -> str:
    """处理 repl 聊天循环中的单个动作：护栏检查 → 执行 → 结果回灌。

    返回用于 tracer 记录的结果摘要。
    """
    if action.type == 'done':
        answer = action.answer or '完成'
        print(f"{ui.dot()} {ui.paint('bold', 'Done')}")
        for line in answer.splitlines() or ['(无答案)']:
            print(f'  {line}')
        # done 也是一次 tool call，回灌 ToolMessage 保持协议完整（会话可能被 /resume 续跑）
        if action.tool_call_id:
            messages.append(ToolMessage(content=answer, tool_call_id=action.tool_call_id))
        return answer

    if action.type == 'take_note':
        key = action.note_key or ''
        value = action.note_value or ''
        if key:
            memory.write(key, value)
            memory.consolidate()
        result_text = f'已记录: {key}={value[:50] if value else ""}'
        print(f"{ui.dot()} {ui.paint('bold', 'Note')}({key})")
        print(ui.render_result_line(value or '(空)', True))
        if action.tool_call_id:
            messages.append(ToolMessage(content=result_text, tool_call_id=action.tool_call_id))
        else:
            messages.append(UserMessage(content=result_text))
        return result_text

    if action.type != 'call_tool':
        msg = f'未知动作类型: {action.type}'
        print(f"  {ui.paint('yellow', '⚠')} {msg}")
        messages.append(UserMessage(content=msg))
        return msg

    t_name = action.tool or ''
    t_args = action.args or {}
    _print_tool_call(action)

    if mock:
        print(ui.render_result_line('(mock)', True))
        if action.tool_call_id:
            messages.append(ToolMessage(content='(mock)', tool_call_id=action.tool_call_id))
        return '(mock)'

    # 与 batch 模式一致：工具执行前必须过安全护栏
    guard_result = guardrail(action)
    if guard_result.disposition == 'deny':
        msg = f'⛔ 安全护栏: {guard_result.reason}'
        print(ui.render_result_line(msg, ok=False))
        if action.tool_call_id:
            messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
        else:
            messages.append(UserMessage(content=msg))
        return f'[DENIED] {guard_result.reason}'

    if guard_result.disposition == 'escalate':
        if state is not None and auto_approve_escalate(state.get('mode', 'ask')):
            approved = True
            print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', 'yolo 模式自动批准')}")
        else:
            try:
                answer = input(ui.paint('yellow', f'  ⚡ {guard_result.reason} · 批准执行? [y/N] '))
                approved = answer.strip().lower() in ('y', 'yes')
            except (EOFError, KeyboardInterrupt):
                approved = False
                print()
        if not approved:
            msg = f'操作被拒绝: {guard_result.reason}'
            print(ui.render_result_line(msg, ok=False))
            if action.tool_call_id:
                messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
            else:
                messages.append(UserMessage(content=msg))
            return f'[ESCALATED-DENIED] {guard_result.reason}'
        if not (state and auto_approve_escalate(state.get('mode', 'ask'))):
            print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', '已人工批准')}")

    # ask 模式：文件改动前逐次确认（'a' 可切换为本会话自动允许编辑）
    if state is not None and needs_confirm(state.get('mode', 'ask'), t_name):
        write_path = t_args.get('path', '')
        try:
            ans = input(f"  ⚡ 将写入文件 {write_path}，允许? [y/N/a] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            ans = ''
            print()
        if ans == 'a':
            state['mode'] = 'accept'
            print(f"  {ui.paint('green', '✓')} {chip('accept')[0]} — 本会话后续编辑自动允许")
        elif ans != 'y':
            msg = f'用户拒绝本次写入: {write_path}'
            print(ui.render_result_line(msg, ok=False))
            if action.tool_call_id:
                messages.append(ToolMessage(content=msg, tool_call_id=action.tool_call_id))
            else:
                messages.append(UserMessage(content=msg))
            return f'[USER-DENIED] {write_path}'

    try:
        tr = tool_registry.execute(t_name, t_args)
    except Exception as e:
        print(f"  {ui.paint('yellow', '⚠')} {e}")
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


def _pick_session(store: SessionStore, exclude_id: str | None = None) -> Session | None:
    """列出历史会话供选择（/resume）：TTY 用方向键，非 TTY 输入序号。"""
    sessions = [s for s in store.list() if s.data.get('id') != exclude_id]
    if not sessions:
        print('  暂无历史会话。')
        return None
    options = [s.data.get('title', DEFAULT_TITLE) for s in sessions]
    hints = [f"{time_ago(s.data.get('updated_at', ''))} · {len(s.memory)} 条笔记 · "
             f"{s.data.get('workspace', '')}" for s in sessions]
    idx = term.choose(options, title=f'恢复会话（共 {len(sessions)} 个）', hints=hints)
    if idx is None:
        print('  已取消。')
        return None
    return sessions[idx]


def repl(mock: bool = False, max_steps: int = 30, resume: bool = False):
    """交互式 REPL 主循环 — 持续对话模式。

    会话模型：新会话的记忆从零开始（不注入历史），每个会话独立落盘；
    /resume 或 --resume 可恢复之前会话的记忆与对话上下文。
    """
    ensure_config()
    config = load_config()
    tracer = Tracer(config['traces_dir'])
    store = SessionStore()

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
        _attach_governance_tools(tool_registry)
        tools = tool_registry.list()

    # 新会话：记忆从零开始
    session = store.new(workspace=config['workspace'])
    memory = SessionMemory(session, live_messages=session.messages)
    messages = session.messages

    def _switch_to(target: Session):
        nonlocal session, memory, messages
        session = target
        memory = SessionMemory(session, live_messages=session.messages)
        messages = session.messages

    _print_banner(mock=mock, config=config)

    if resume:
        picked = _pick_session(store)
        if picked is not None:
            _switch_to(picked)
            print(f"  ✓ 已恢复「{picked.data['title']}」（{len(messages)} 条消息，{len(picked.memory)} 条笔记）")

    # 权限模式（Shift+Tab 循环）：ask 默认逐次询问文件改动；accept 允许编辑；yolo 全部允许
    state = {'mode': 'ask'}
    history: list[str] = []

    def _on_shift_tab() -> str:
        state['mode'] = cycle(state['mode'])
        return '> '

    def _footer():
        text, style = chip(state['mode'])
        return [(text, style)]

    def _rule_fn(buf: str = '') -> str:
        """输入框上下线：铺满整个终端宽度。"""
        return ui.paint('dim', '-' * max(20, shutil.get_terminal_size((80, 24)).columns - 1))

    try:
        while True:
            rule = _rule_fn('')
            try:
                # 输入框钉在终端底部；内容正常进回滚区，可随意上翻
                term.prepare_pinned_prompt(rule, rule_fn=_rule_fn)
                text = term.read_line(
                    '> ', history=history,
                    on_shift_tab=_on_shift_tab, footer_fn=_footer,
                    rule_fn=_rule_fn).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                break
            if not text:
                continue
            term.clear_pinned_box()
            print(ui.band(text))

            intent = _parse_intent(text)

            if intent['type'] == 'exit':
                print('  再见！')
                break

            elif intent['type'] == 'help':
                _show_help()

            elif intent['type'] == 'show_config':
                _show_config()

            elif intent['type'] == 'resume':
                picked = _pick_session(store, exclude_id=session.data["id"])
                if picked is not None:
                    _switch_to(picked)
                    print(f"  ✓ 已恢复「{picked.data['title']}」"
                          f"（{len(messages)} 条消息，{len(picked.memory)} 条笔记）")

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
                print(f"  {ui.paint('green', '✓')} 已更新 {display} = {shown}")

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
                    session.note_title(f"/run {intent.get('file', '')}")
                else:
                    chat_text = intent.get('text', '')
                    session.note_title(chat_text)
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
                            memory, mock, state,
                        )
                        tracer.record(step, action, result=result_summary)
                        if action.type == 'done':
                            any_done = True
                            break
                    if any_done:
                        break
                # 每轮对话结束落盘（记忆 + 消息历史）
                session.save(messages)
    finally:
        session.save(messages)
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
        '--resume',
        action='store_true',
        help='启动时选择恢复一个历史会话',
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
    _attach_governance_tools(tool_registry)
    workspace = config['workspace']

    # batch 模式：每次任务运行也是一个独立会话
    store = SessionStore()
    session = store.new(workspace=workspace)
    session.note_title(args.name or 'batch 任务')
    memory = SessionMemory(session)
    tracer = Tracer(config['traces_dir'])

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
    session.save()
    return answer


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.file:
        repl(mock=args.mock, max_steps=args.max_steps, resume=args.resume)
        return

    answer = run(args)
    print(f'\n结果: {answer}')


if __name__ == '__main__':
    main()
