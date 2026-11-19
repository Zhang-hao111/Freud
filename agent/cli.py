"""CLI 入口 — 命令行解析 + 交互式 REPL，支持斜杠命令和自然语言混合输入。"""

import argparse
import re
import shutil
import sys
from pathlib import Path

from agent.config import load_config, save_config, ensure_config
from agent.core import (run_agent, _print_tool_call, _print_result,
                        build_chat_system_prompt)
from agent import term, ui
from agent.llm import MockLLM
from agent.permissions import chip, cycle
from agent.runtime import default_runtime
from agent.tool_execution import record_tool_reply
from agent.session import DEFAULT_TITLE, Session, SessionConflict, SessionStore, SessionMemory, time_ago
from agent.types import (
    Action, SystemMessage, UserMessage,
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


def _console_approver(action: Action) -> bool:
    """batch 模式下 escalate 操作的人工确认。"""
    args = action.args or {}
    try:
        ans = input(f'  ⚡ 危险操作待人工确认: {action.tool}({args})。批准执行? [y/N] ')
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return ans.strip().lower() in ('y', 'yes')


def _repl_handle_action(action, messages, runtime, registry, memory, mock, state):
    """将执行服务的结果显示在终端，再回灌模型。"""
    if action.type == 'done':
        result = summary = action.answer or '完成'
        print(f"{ui.dot()} {ui.paint('bold', 'Done')}")
        for line in result.splitlines() or ['(无答案)']:
            print(f'  {line}')
    elif action.type == 'take_note':
        key, value = action.note_key or '', action.note_value or ''
        if key:
            memory.write(key, value)
            memory.consolidate()
        result = summary = f'已记录: {key}={value[:50]}'
        print(f"{ui.dot()} {ui.paint('bold', 'Note')}({key})")
        print(ui.render_result_line(value or '(空)', True))
    elif action.type == 'call_tool':
        def approve(action, gate, reason):
            if gate in ('escalate', 'shell'):
                try:
                    answer = input(ui.paint('yellow', f'  ⚡ {reason} · 批准执行? [y/N] '))
                except (EOFError, KeyboardInterrupt):
                    print()
                    return False
                if answer.strip().lower() in ('y', 'yes'):
                    print(f"  {ui.paint('yellow', '⚡')} {ui.paint('dim', '已人工批准')}")
                    return True
                return False

            path = (action.args or {}).get('path', '')
            try:
                answer = input(f"  ⚡ 将写入文件 {path}，允许? [y/N/a] ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                print()
                return False
            if answer == 'a':
                state['mode'] = 'accept'
                print(f"  {ui.paint('green', '✓')} {chip('accept')[0]} — 本会话后续编辑自动允许")
            return answer in ('a', 'y')

        _print_tool_call(action)
        outcome = runtime['execute_tool'](registry, action, approve=approve,
                                          mode=state['mode'] if not mock else None, mock=mock)
        result = summary = outcome.content
        if outcome.status == 'blocked':
            result = f'⛔ 安全护栏: {outcome.content}'
            summary = f'[DENIED] {outcome.content}'
        elif outcome.status == 'declined':
            if outcome.gate == 'edit':
                result = f'用户拒绝本次写入: {outcome.content}'
                summary = f'[USER-DENIED] {outcome.content}'
            else:
                result = f'操作被拒绝: {outcome.content}'
                summary = f'[ESCALATED-DENIED] {outcome.content}'
        elif outcome.status in ('invalid', 'error'):
            summary = f'[EXC] {outcome.content}'
        elif outcome.status == 'mock':
            result = summary = '(mock)'
        _print_result(action.tool or '', outcome.success, result)
    else:
        result = summary = f'未知动作类型: {action.type}'
        print(f"  {ui.paint('yellow', '⚠')} {result}")
    record_tool_reply(messages, action, result)
    return summary


def _pick_session(store: SessionStore, exclude_id: str | None = None) -> Session | None:
    """列出历史会话供选择（/resume）：TTY 用方向键，非 TTY 输入序号。"""
    sessions = [row for row in store.summaries() if row['id'] != exclude_id]
    if not sessions:
        print('  暂无历史会话。')
        return None
    options = [row.get('title') or DEFAULT_TITLE for row in sessions]
    hints = [f"{time_ago(row.get('updated_at') or '')} · {row['memory_count']} 条笔记 · "
             f"{row.get('workspace') or ''}" for row in sessions]
    idx = term.choose(options, title=f'恢复会话（共 {len(sessions)} 个）', hints=hints)
    if idx is None:
        print('  已取消。')
        return None
    return store.get(sessions[idx]['id'])


def repl(mock: bool = False, max_steps: int | None = None, resume: bool = False):
    """交互式 REPL 主循环 — 持续对话模式。

    会话模型：新会话的记忆从零开始（不注入历史），每个会话独立落盘；
    /resume 或 --resume 可恢复之前会话的记忆与对话上下文。
    """
    ensure_config()
    config = load_config()
    max_steps = config['max_steps'] if max_steps is None else max_steps
    if not mock and not config.get('api_key'):
        print('[!] 错误：未设置 API Key。')
        print('   请先配置: /key = sk-xxx')
        return
    runtime = default_runtime(config, mock=mock)
    store = runtime['sessions']
    models = runtime['models']
    mock_model = MockLLM(responses=[]) if mock else None

    # 新会话：记忆从零开始
    session = store.new(workspace=str(Path(config['workspace']).expanduser().resolve()))
    tracer = runtime['tracer'](session.data['id'])

    def _switch_to(target: Session):
        nonlocal session, tracer
        session = target
        tracer = runtime['tracer'](session.data['id'])
        print(f"  ✓ 已恢复「{target.data['title']}」"
              f"（{len(target.messages)} 条消息，{len(target.memory)} 条笔记）")

    _print_banner(mock=mock, config=config)

    if resume:
        picked = _pick_session(store)
        if picked is not None:
            _switch_to(picked)

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

            elif intent['type'] == 'set':
                cfg_key = intent['key']
                cfg_value = intent['value']
                save_config({cfg_key: cfg_value})
                # 同步更新内存中的 config，否则本次会话重建 Provider 时仍用旧配置
                config[cfg_key] = cfg_value
                # 如果 LLM 已初始化，动态更新配置
                if not mock:
                    models.reload()
                display = DISPLAY_NAMES.get(cfg_key, cfg_key)
                shown = _mask_secret(cfg_value) if cfg_key == 'api_key' else cfg_value
                print(f"  {ui.paint('green', '✓')} 已更新 {display} = {shown}")

            elif intent['type'] in ('chat', 'run'):
                if intent['type'] == 'run':
                    # /run 读取任务文件内容交给 LLM，而不是把路径字符串发过去
                    run_file = Path(intent.get('file', ''))
                    if not run_file.exists():
                        print(f'  ⚠ 任务文件不存在: {run_file}')
                        continue
                    chat_text = run_file.read_text(encoding='utf-8')
                    title = f"/run {intent.get('file', '')}"
                else:
                    title = chat_text = intent.get('text', '')

                try:
                    with session.turn():
                        messages = session.messages
                        memory = SessionMemory(session, live_messages=messages)
                        workspace = session.data.get('workspace') or config['workspace']
                        registry = runtime['tools'](workspace)
                        tools = [] if mock else registry.list()
                        if not messages:
                            messages.append(SystemMessage(content=build_chat_system_prompt(workspace)))
                        session.note_title(title)
                        messages.append(UserMessage(content=chat_text))
                        action_step = 0

                        def handle_action(_, action):
                            nonlocal action_step
                            action_step += 1
                            summary = _repl_handle_action(action, messages, runtime, registry, memory, mock, state)
                            tracer.record(action_step, action, result=summary)

                        try:
                            finished = runtime['chat_loop'](messages, models.chat(mock_model), tools, handle_action,
                                                            lambda reply: print(f'\n{reply}\n'), max_steps=max_steps, memory=memory)
                            if not finished:
                                print(f'  ⚠ 已达到最大执行步数 ({max_steps})。')
                        except Exception as error:
                            print(f'  ⚠ LLM 调用失败: {error}')
                        finally:
                            session.save(messages)
                            tracer.flush()
                except SessionConflict as error:
                    print(f'  ⚠ {error}')
    except KeyboardInterrupt:
        print()

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
        default=None,
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

    if not args.mock and not config.get('api_key'):
        print('错误：未设置 API Key')
        sys.exit(1)

    try:
        goal = Path(args.file).read_text(encoding='utf-8')
    except FileNotFoundError:
        print(f'文件未找到: {args.file}')
        sys.exit(1)

    if not goal.strip():
        print('任务描述为空')
        sys.exit(1)

    runtime = default_runtime(config, mock=args.mock)
    workspace = str(Path(config['workspace']).expanduser().resolve())

    # batch 模式：每次任务运行也是一个独立会话
    session = runtime['sessions'].new(workspace=workspace)
    # escalate 审批策略：--yes 全自动批准；交互终端人工确认；非交互默认拒绝
    if args.yes:
        approver = lambda action: True
    elif sys.stdin.isatty():
        approver = _console_approver
    else:
        approver = None

    with session.turn():
        session.note_title(args.name or 'batch 任务')
        messages = session.messages
        return run_agent(
            goal=goal,
            task_name=args.name,
            llm=runtime['models'].chat(MockLLM(responses=[]) if args.mock else None),
            tool_registry=runtime['tools'](workspace),
            memory=SessionMemory(session, live_messages=messages),
            tracer=runtime['tracer'](session.data['id']),
            max_steps=config['max_steps'] if args.max_steps is None else args.max_steps,
            approver=approver,
            workspace=workspace,
            tool_executor=runtime['execute_tool'],
            chat_loop=runtime['chat_loop'],
            messages=messages,
        )


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.max_steps is not None and args.max_steps < 1:
        parser.error('--max-steps 必须大于 0')
    try:
        if not args.file:
            repl(mock=args.mock, max_steps=args.max_steps, resume=args.resume)
        else:
            answer = run(args)
            print(f'\n结果: {answer}')
    except SessionConflict as error:
        parser.exit(1, f'{error}\n')


if __name__ == '__main__':
    main()
