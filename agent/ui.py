"""终端 UI 辅助 — Claude Code 风格的输出元素（⏺ 工具调用行 / ⎿ 结果行）。

- ANSI 颜色仅在 TTY 且未设置 NO_COLOR / TERM=dumb 时启用，管道输出自动降级为纯文本
- 提供东亚宽度感知的对齐（中文按两列计），保证含中文的盒线/表头对齐
"""

import os
import re
import shutil
import sys
import unicodedata

_RESET = '\x1b[0m'
_STYLES = {
    'bold': '1', 'dim': '2', 'italic': '3',
    'red': '31', 'green': '32', 'yellow': '33', 'cyan': '36', 'gray': '90',
}

# 主题强调色：粉色（真彩色优先，256 色兜底）
_ACCENT_TRUE = '\x1b[38;2;255;105;180m'
_ACCENT_256 = '\x1b[38;5;205m'

_enabled: bool | None = None
_ambig_wide_cache: bool | None = None

DOT = '⏺'
ELBOW = '⎿'

DISPLAY_NAMES = {
    'read_file': 'Read',
    'write_file': 'Write',
    'shell': 'Bash',
    'done': 'Done',
}


def detect_enabled() -> bool:
    """仅在交互终端且未被禁用时启用颜色。"""
    if os.environ.get('NO_COLOR'):
        return False
    if os.environ.get('TERM') == 'dumb':
        return False
    return sys.stdout.isatty()


def enabled() -> bool:
    global _enabled
    if _enabled is None:
        _enabled = detect_enabled()
    return _enabled


def set_enabled(value: bool) -> None:
    """手动覆盖颜色开关（测试用）。"""
    global _enabled
    _enabled = value


def _wrap(code: str, text: str) -> str:
    return f'{code}{text}{_RESET}'


def paint(style: str, text: str) -> str:
    """按样式着色；未启用时原样返回。"""
    if not enabled():
        return text
    code = _STYLES.get(style)
    return _wrap(f'\x1b[{code}m', text) if code else text


def dot() -> str:
    """粉色工具调用标记 ⏺。"""
    if not enabled():
        return DOT
    return _wrap(_accent_code(), DOT)


def _accent_code() -> str:
    return _ACCENT_TRUE if os.environ.get('COLORTERM') in ('truecolor', '24bit') else _ACCENT_256


def paint_accent(text: str) -> str:
    """整段按主题强调色（粉色）着色（吉祥物/强调用）。"""
    if not enabled():
        return text
    return _wrap(_accent_code(), text)


# 兼容旧名
paint_orange = paint_accent


def elbow() -> str:
    """结果行前缀 ⎿。"""
    return paint('dim', ELBOW)


def _ambig_wide() -> bool:
    """当前 locale 是否把宽度歧义字符（█ ─ ■ 等）按 2 列渲染。

    CJK（zh/ja/ko）locale 下终端通常将歧义宽度字符渲染为全角，
    据此决定 visual_width 的口径，保证含块字符的排版对齐。
    """
    global _ambig_wide_cache
    if _ambig_wide_cache is None:
        lang = ''
        for var in ('LC_ALL', 'LC_CTYPE', 'LANG'):
            val = os.environ.get(var)
            if val:
                lang = val.lower()
                break
        _ambig_wide_cache = lang.split('.')[0].split('@')[0][:2] in ('zh', 'ja', 'ko')
    return _ambig_wide_cache


def visual_width(text: str) -> int:
    """字符串的终端显示宽度（全角 2 列；歧义宽度字符随 CJK locale 判定）。"""
    ambig = 2 if _ambig_wide() else 1
    total = 0
    for ch in text:
        ea = unicodedata.east_asian_width(ch)
        if ea in ('W', 'F'):
            total += 2
        elif ea == 'A':
            total += ambig
        else:
            total += 1
    return total


def pad(text: str, width: int) -> str:
    """按显示宽度左对齐补空格（中文对齐专用）。"""
    return text + ' ' * max(0, width - visual_width(text))


def _truncate(text: str, limit: int) -> str:
    return text if visual_width(text) <= limit else text[:limit] + '…'


def visual_clip(text: str, limit: int) -> str:
    """按显示宽度截断（不加省略号，用于单行编辑器）。"""
    if limit <= 0:
        return ''
    out, width = '', 0
    for ch in text:
        cw = visual_width(ch)
        if width + cw > limit:
            break
        out += ch
        width += cw
    return out


def band(text: str) -> str:
    """整行灰底的用户消息条（Claude Code 提交样式）。非 TTY 退化为纯文本。"""
    width = shutil.get_terminal_size((80, 24)).columns - 1
    inner = visual_clip(' ❯ ' + text, width)
    if not enabled():
        return inner
    pad = ' ' * max(0, width - visual_width(inner))
    return f'\x1b[48;5;236m{inner}{pad}\x1b[0m'


def ambiguous_wide() -> bool:
    """当前 locale 是否将宽度歧义字符按 2 列渲染（供外部模块对齐用）。"""
    return _ambig_wide()


def tool_display_name(tool: str) -> str:
    return DISPLAY_NAMES.get(tool, tool)


def _arg_preview(tool: str, args: dict) -> str:
    args = args or {}
    if tool in ('read_file', 'write_file'):
        return str(args.get('path', ''))
    if tool == 'shell':
        return str(args.get('command', ''))
    return str(args)


def render_tool_line(tool: str, args: dict) -> str:
    """渲染工具调用行：⏺ Bash(ls -la)"""
    name = tool_display_name(tool)
    preview = _truncate(_arg_preview(tool, args), 100)
    return f'{dot()} {paint("bold", name)}({preview})'


def render_result_line(text: str, ok: bool = True, limit: int = 200) -> str:
    """渲染结果预览行：⎿ 输出摘要（折叠空白、超长截断）。"""
    body = ' '.join(str(text).split()) or '(无输出)'
    extra = ''
    if len(body) > limit:
        extra = f' …(+{len(body) - limit} 字符)'
        body = body[:limit]
    return f'{elbow()} {paint("dim" if ok else "red", body)}{paint("dim", extra)}'
