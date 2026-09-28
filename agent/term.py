"""终端交互层 — raw-mode 行编辑器、方向键选择器、底部 footer。

设计约束：
- 仅在 TTY 上启用 cbreak/raw 键盘读取；管道/测试环境自动降级为 input()/数字选择；
- 行编辑支持：普通字符、退格、←→移动、Home/End、↑/↓ 输入历史、Tab 接受暗色提示、
  Shift+Tab 触发 on_shift_tab 回调（切权限模式），footer 行紧贴输入行下方实时刷新；
- Ctrl+C 走 SIGINT（KeyboardInterrupt 由调用方处理）；Ctrl+D 返回空串。
"""

import os
import re
import select
import shutil
import sys
import time

from agent import ui

try:
    import termios
    import tty
    _POSIX = True
except ImportError:  # pragma: no cover
    _POSIX = False

ESC = '\x1b'
_SHIFT_TAB = '\x1b[Z'
_ARROWS = {'A': 'up', 'B': 'down', 'C': 'right', 'D': 'left', 'H': 'home', 'F': 'end'}
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')


class _RawMode:
    """进入/退出 cbreak 模式（Ctrl+C 仍产生 SIGINT，由调用方捕获）。"""

    def __init__(self):
        self._active = _POSIX and sys.stdin.isatty()
        self._saved = None

    def __enter__(self):
        if self._active:
            self._saved = termios.tcgetattr(sys.stdin.fileno())
            tty.setcbreak(sys.stdin.fileno())
        return self

    def __exit__(self, *exc):
        if self._active:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, self._saved)
        return False


def _read_key() -> str:
    """读取一个按键，返回规范化键名。

    全部通过 os.read 直读 fd（绕开 TextIOWrapper 的内部预读缓冲），
    否则 select 会与缓冲区脱节、ESC 序列被拆散；多字节 UTF-8（中文输入）
    按首字节长度一次性取齐。
    """
    b = os.read(0, 1)
    if b == b'':
        return 'eof'
    n = 0
    if b[0] & 0b11100000 == 0b11000000:
        n = 1
    elif b[0] & 0b11110000 == 0b11100000:
        n = 2
    elif b[0] & 0b11111000 == 0b11110000:
        n = 3
    if n:
        while len(b) < n + 1:
            nxt = os.read(0, n + 1 - len(b))
            if not nxt:
                break
            b += nxt
    ch = b.decode('utf-8', errors='replace')
    if ch == ESC:
        seq = ch
        while True:
            nxt = _read_nonblocking()
            if nxt is None:
                return 'esc' if seq == ESC else seq
            seq += nxt
            if seq == _SHIFT_TAB:
                return 'shift_tab'
            if seq.startswith('\x1b[') and len(seq) >= 3:
                return _ARROWS.get(seq[2], seq)
            if len(seq) > 8:
                return seq
    if ch in ('\r', '\n'):
        return 'enter'
    if ch == '\x7f':
        return 'backspace'
    if ch == '\x04':
        return 'ctrl_d'
    return ch


def _read_nonblocking() -> str | None:
    import select
    ready, _, _ = select.select([0], [], [], 0.02)
    if ready:
        return os.read(0, 1).decode('utf-8', errors='replace')
    return None


def _term_width() -> int:
    return shutil.get_terminal_size((80, 24)).columns


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


# ---- 行编辑器 ----

def read_line(prompt: str = '> ',
              history: list[str] | None = None,
              on_shift_tab=None,
              footer_fn=None,
              rule_fn=None,
              hint: str | None = None) -> str:
    """读取一行输入（TTY 下 raw 模式；非 TTY 降级为 input()）。

    on_shift_tab: 无参回调，返回新的提示字符串（已着色），用于权限模式切换。
    footer_fn:    无参回调，返回 [(文本, 样式), ...]，渲染为输入框下的 footer 行。
    rule_fn:      无参回调，返回随输入变化的框线文本（钉住模式下每次重绘更新）。
    hint:         输入为空时显示的暗色建议文本，Tab 接受。
    """
    if not (_POSIX and sys.stdin.isatty()):
        return input(prompt)

    history = list(history or [])
    buf = ''
    cursor = 0
    hist_idx = len(history)
    draft = ''
    cur_prompt = prompt

    def _paint():
        """钉住模式：绝对定位重绘 上下框线/输入行/footer 四行；
        非钉住模式：仅在当前行重绘 + 下方一行 footer。"""
        if _pin['active']:
            if rule_fn:
                sys.stdout.write(f'\x1b[{_pin["rule"]};1H\x1b[K' + rule_fn(buf))
            shown = ui.visual_clip(buf, max(0, _term_width() - 4
                                            - ui.visual_width(_strip_ansi(cur_prompt))))
            sys.stdout.write(f'\x1b[{_pin["input"]};1H\x1b[K' + cur_prompt + shown)
            if hint and not buf:
                sys.stdout.write(ui.paint('dim', hint))
            segs = footer_fn() if footer_fn else []
            sys.stdout.write(f'\x1b[{_pin["footer"]};1H\x1b[K'
                             + (render_footer(segs) if segs else ''))
            col = ui.visual_width(_strip_ansi(cur_prompt)) + ui.visual_width(buf[:cursor])
            sys.stdout.write(f'\x1b[{_pin["input"]};{col + 1}H')
            sys.stdout.flush()
        else:
            _redraw_prompt(cur_prompt, buf, cursor, hint if not buf else None)
            if footer_fn:
                segs = footer_fn()
                sys.stdout.write('\n\r\x1b[K' + render_footer(segs))
                col = ui.visual_width(_strip_ansi(cur_prompt)) + ui.visual_width(buf[:cursor])
                sys.stdout.write(f'\x1b[1A\r\x1b[{col}C')
            sys.stdout.flush()

    with _RawMode():
        while True:
            _paint()
            key = _read_key()

            if key == 'enter':
                if _pin['active']:
                    # 清输入行与 footer 行，光标停在输入行（随后由调用方清框）
                    sys.stdout.write(f'\x1b[{_pin["input"]};1H\x1b[K')
                    sys.stdout.write(f'\x1b[{_pin["footer"]};1H\x1b[K')
                    sys.stdout.write(f'\x1b[{_pin["input"]};1H')
                else:
                    sys.stdout.write('\r\x1b[K\n')
                if buf:
                    history.append(buf)
                return buf

            if key == 'ctrl_d' or key == 'eof':
                sys.stdout.write('\r\x1b[K\n')
                raise EOFError

            if key == 'shift_tab':
                if on_shift_tab:
                    cur_prompt = on_shift_tab()
                continue

            if key == 'backspace':
                if cursor > 0:
                    buf = buf[:cursor - 1] + buf[cursor:]
                    cursor -= 1
                continue

            if key == 'left':
                cursor = max(0, cursor - 1)
                continue
            if key == 'right':
                cursor = min(len(buf), cursor + 1)
                continue
            if key in ('home',):
                cursor = 0
                continue
            if key in ('end',):
                cursor = len(buf)
                continue

            if key == 'up':
                if hist_idx > 0:
                    if hist_idx == len(history):
                        draft = buf
                    hist_idx -= 1
                    buf = history[hist_idx]
                    cursor = len(buf)
                continue
            if key == 'down':
                if hist_idx < len(history):
                    hist_idx += 1
                    buf = draft if hist_idx == len(history) else history[hist_idx]
                    cursor = len(buf)
                continue

            if key == 'esc':
                continue

            if key == '\t' and hint and not buf:
                buf = hint
                cursor = len(buf)
                continue

            if isinstance(key, str) and key >= ' ':
                buf = buf[:cursor] + key + buf[cursor:]
                cursor += 1


def _redraw_prompt(prompt: str, buf: str, cursor: int, hint: str | None):
    """单行重绘：\r 回行首清行，写 提示 + 已输入 + 暗色 hint。"""
    width = _term_width() - 4
    shown = ui.visual_clip(buf, max(0, width - ui.visual_width(_strip_ansi(prompt))))
    sys.stdout.write('\r\x1b[K' + prompt + shown)
    if hint and not buf:
        sys.stdout.write(ui.paint('dim', hint))
    sys.stdout.flush()


# ---- 方向键选择器 ----

def choose(options: list[str], title: str = '', start: int = 0,
           hints: list[str] | None = None) -> int | None:
    """方向键选择器：↑/↓ 移动，回车确认返回索引，Esc/q/Ctrl+C 取消返回 None。

    非 TTY 环境降级为"输入序号"。
    """
    if not options:
        return None

    if not (_POSIX and sys.stdin.isatty()):
        print('  可选: ' + ' | '.join(f'{i + 1}.{o}' for i, o in enumerate(options)))
        choice = input('  输入序号（回车取消）: ').strip()
        if choice.isdigit() and 1 <= int(choice) <= len(options):
            return int(choice) - 1
        return None

    idx = max(0, min(start, len(options) - 1))
    hints = hints or []

    def _render(with_title: bool):
        if with_title and title:
            print('\r\x1b[K' + ui.paint('bold', title))
        for i, opt in enumerate(options):
            marker = ui.paint_accent('❯ ') if i == idx else '  '
            line = marker + (ui.paint('bold', opt) if i == idx else opt)
            if i < len(hints) and hints[i]:
                line += '  ' + ui.paint('dim', hints[i])
            print('\r\x1b[K' + line)
        sys.stdout.flush()

    print()
    with _RawMode():
        _render(with_title=True)
        while True:
            key = _read_key()
            if key == 'up':
                idx = (idx - 1) % len(options)
                sys.stdout.write(f'\x1b[{len(options)}A')
                _render(with_title=False)
            elif key == 'down':
                idx = (idx + 1) % len(options)
                sys.stdout.write(f'\x1b[{len(options)}A')
                _render(with_title=False)
            elif key == 'enter':
                sys.stdout.write('\n')
                return idx
            elif key in ('esc', 'q', 'ctrl_d', 'eof'):
                sys.stdout.write('\n')
                return None


# ---- 底部固定输入框（光标查询 + 底部重绘，保留终端回滚） ----

# 行分配：h-4 顶部线、h-3 输入行、h-2 底部线、h-1 footer；内容照常进终端回滚区
_pin = {'active': False, 'rule': 0, 'input': 0, 'footer': 0}


def _tty() -> bool:
    return _POSIX and sys.stdin.isatty() and sys.stdout.isatty()


def query_cursor_row(timeout: float = 0.3) -> int | None:
    """向终端查询光标所在行号（DSR-CPR）。非 TTY 或超时返回 None。"""
    if not _tty():
        return None
    with _RawMode():
        sys.stdout.write('\x1b[6n')
        sys.stdout.flush()
        deadline = time.time() + timeout
        buf = ''
        while time.time() < deadline:
            r, _, _ = select.select([0], [], [], 0.05)
            if r:
                buf += os.read(0, 64).decode('utf-8', errors='replace')
                m = re.search(r'\x1b\[(\d+);(\d+)R', buf)
                if m:
                    return int(m.group(1))
    return None


def prepare_pinned_prompt(rule: str, rule_fn=None) -> None:
    """把输入框钉到终端底部。

    查询当前光标行：不足底部预留行就用换行补齐；超出则整屏上滚
    （内容照常进终端回滚区，仅顶部损失少量行）。随后渲染上下两条
    框线并清出输入行与 footer 行。非 TTY 或查询失败时退化为普通打印。
    rule_fn: 无参回调，返回随输入变化的框线文本（重绘时更新）。
    """
    if not _tty():
        print('\n' + rule)
        return
    h = shutil.get_terminal_size((80, 24)).lines
    row = query_cursor_row()
    if row is None or h < 8:
        print('\n' + rule)
        return
    rule_row, input_row, rule2_row, footer_row = h - 4, h - 3, h - 2, h - 1
    _pin.update(active=True, rule=rule_row, input=input_row, footer=footer_row)
    target = rule_row
    if row > target:
        sys.stdout.write(f'\x1b[{row - target}S')
    elif row < target:
        sys.stdout.write('\r' + '\n' * (target - row))
    sys.stdout.write(f'\x1b[{rule_row};1H\x1b[K' + rule)
    sys.stdout.write(f'\x1b[{input_row};1H\x1b[K')
    sys.stdout.write(f'\x1b[{rule2_row};1H\x1b[K' + (rule_fn('') if rule_fn else rule))
    sys.stdout.write(f'\x1b[{footer_row};1H\x1b[K')
    sys.stdout.write(f'\x1b[{input_row};1H')
    sys.stdout.flush()


def clear_pinned_box() -> None:
    """提交后清除固定输入框全部四行，光标停在框首行（原位置开始输出）。"""
    if not _tty():
        return
    h = shutil.get_terminal_size((80, 24)).lines
    sys.stdout.write(f'\x1b[{h - 4};1H\x1b[J')
    sys.stdout.flush()
    _pin['active'] = False


# ---- 底部 footer ----

def render_footer(left: list[tuple[str, str]], right: list[tuple[str, str]] | None = None) -> str:
    """渲染 footer：仅模式徽标等内容段，无填充线。"""
    return '  '.join(ui.paint(style, text) for text, style in left)


def write_footer(left, right=None):
    print(render_footer(left, right))
