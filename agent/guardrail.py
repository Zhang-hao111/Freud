"""Guardrail（安全护栏）— 对 Shell 命令进行三态安全检查，拦截逻辑是代码而非提示词。"""

import re
from collections.abc import Callable
from agent.types import Action, GuardrailResult, Disposition, DangerousPattern

# 默认危险模式表 — 三态分级
DEFAULT_DANGEROUS_PATTERNS: list[DangerousPattern] = [
    # escalate — 可能破坏系统，需人工确认
    DangerousPattern(r'rm\s+-rf\s+/', 'escalate', '删除文件系统'),
    DangerousPattern(r'mkfs', 'escalate', '格式化磁盘'),
    DangerousPattern(r'dd\s+if=', 'escalate', '覆写磁盘'),
    DangerousPattern(r'>\s*/dev/sda', 'escalate', '覆写磁盘'),
    DangerousPattern(r'fdisk', 'escalate', '修改分区表'),
    # deny — 直接拒绝
    DangerousPattern(r':\(\)\{\s*:\|:\&', 'deny', '检测到 fork 炸弹'),
]

# Approver 类型：接收 Action，返回是否批准
Approver = Callable[[Action], bool] | None  # 实际类型: Callable[[Action], bool]

def guardrail(
    action: Action,
    patterns: list[DangerousPattern] | None = None,
) -> GuardrailResult:
    """Guardrail 纯函数 — 对 Action 进行确定性安全检测。

    只对 shell 命令做检查；非 shell 类型的 call_tool 直接 allow。
    """
    if action.type != 'call_tool' or action.tool != 'shell':
        return GuardrailResult(disposition='allow')

    command = action.args.get('command', '') if action.args else ''
    if not isinstance(command, str):
        command = str(command)

    for p in patterns or DEFAULT_DANGEROUS_PATTERNS:
        if re.search(p.pattern, command):
            return GuardrailResult(disposition=p.disposition, reason=p.reason)

    return GuardrailResult(disposition='allow')