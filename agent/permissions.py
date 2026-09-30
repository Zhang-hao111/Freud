"""权限模式 — 三档执行策略（参考 Claude Code），REPL 用 Shift+Tab 循环切换。

- ask:    默认。文件改动（write_file / edit_file）每次询问；shell 按护栏三态规则。
- accept: 允许编辑。write_file / edit_file 直接执行；shell 仍按护栏规则。
- yolo:   允许所有。文件改动与 escalate 级命令全部自动批准（deny 仍拒绝）。

mode 语义已下沉到 core.run_agent：batch 模式同样按三档执行，
REPL 与 batch 共用本模块的 needs_confirm / auto_approve_escalate 判定。
"""

MODES = ('ask', 'accept', 'yolo')

EDIT_TOOLS = ('write_file', 'edit_file')

_CHIPS = {
    'ask': ('⏵⏵ ask before edits', 'yellow'),
    'accept': ('⏵⏵ accept edits on', 'green'),
    'yolo': ('⏵⏵⏵ yolo - auto approve', 'red'),
}


def cycle(mode: str) -> str:
    """Shift+Tab：ask → accept → yolo → ask。"""
    i = MODES.index(mode) if mode in MODES else 0
    return MODES[(i + 1) % len(MODES)]


def chip(mode: str) -> tuple[str, str]:
    """footer 里的模式徽标 (文本, 颜色)。"""
    return _CHIPS.get(mode, _CHIPS['ask'])


def needs_confirm(mode: str, tool: str) -> bool:
    """该工具在当前模式下执行前是否需要用户确认。"""
    if mode == 'accept':
        return False
    if mode == 'ask':
        return tool in EDIT_TOOLS
    return False  # yolo


def auto_approve_escalate(mode: str) -> bool:
    """escalate 级命令是否自动批准。"""
    return mode == 'yolo'
