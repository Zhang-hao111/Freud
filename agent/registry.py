"""工具系统 — read_file / write_file / shell 以及 ToolRegistry。"""

import os
import re
import subprocess

from agent.types import BaseTool, ToolResult


# 单个工具返回给 LLM 的最大字符数，防止一次 cat 大文件撑爆上下文
MAX_TOOL_OUTPUT = 20000


def _clip(text: str) -> str:
    """过长的工具输出截断并附加说明。"""
    if not text or len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + (
        f'\n...[输出过长已截断：原始 {len(text)} 字符，仅保留前 {MAX_TOOL_OUTPUT} 字符]'
    )


# ---- 工具实现 ----


class ReadFileTool(BaseTool):
    name = 'read_file'
    description = '读取指定路径的文件内容'
    parameters = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '文件路径（绝对或相对）'},
        },
        'required': ['path'],
    }

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('path', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 path 参数')
        file_path = os.path.abspath(raw)
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
            return ToolResult(success=True, data=_clip(content))
        except FileNotFoundError:
            return ToolResult(success=False, error=f'文件不存在: {file_path}')
        except Exception as e:
            return ToolResult(success=False, error=f'读取失败: {e}')


class WriteFileTool(BaseTool):
    name = 'write_file'
    description = '将内容写入指定路径的文件，自动创建父目录'
    parameters = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '文件路径（绝对或相对）'},
            'content': {'type': 'string', 'description': '写入的内容'},
        },
        'required': ['path', 'content'],
    }

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('path', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 path 参数')
        content = args.get('content', '')
        if not isinstance(content, str):
            content = str(content)
        file_path = os.path.abspath(raw)
        try:
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            return ToolResult(success=True, data=f'已写入 {file_path}')
        except Exception as e:
            return ToolResult(success=False, error=f'写入失败: {e}')


class ShellTool(BaseTool):
    name = 'shell'
    description = '执行 shell 命令并返回 stdout'
    parameters = {
        'type': 'object',
        'properties': {
            'command': {'type': 'string', 'description': '要执行的 shell 命令'},
        },
        'required': ['command'],
    }

    def __init__(self, timeout: int = 30):
        self.timeout = timeout

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('command', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 command 参数')
        try:
            result = subprocess.run(
                raw,
                shell=True,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
            output = ''
            if result.stdout:
                output += result.stdout
            if result.stderr:
                if output:
                    output += '\n--- stderr ---\n'
                output += result.stderr
            if result.returncode == 0:
                return ToolResult(success=True, data=_clip(output))
            else:
                return ToolResult(
                    success=False,
                    data=_clip(output) if output else '',
                    error=f'返回码 {result.returncode}',
                )
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f'命令执行超时 ({self.timeout}s)')
        except Exception as e:
            return ToolResult(success=False, error=f'执行失败: {e}')


# ---- 工具注册表 ----


class ToolRegistry:
    """管理工具的注册、查找和执行。"""

    def __init__(self):
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool):
        """注册工具，同名工具后注册者胜。"""
        self._tools[tool.name] = tool

    def list(self) -> list[BaseTool]:
        """返回所有已注册工具。"""
        return list(self._tools.values())

    def get(self, name: str) -> BaseTool | None:
        return self._tools.get(name)

    def execute(self, name: str, args: dict) -> ToolResult:
        tool = self.get(name)
        if tool is None:
            raise ValueError(f'未知工具: {name}')
        return tool.execute(args)


def create_default_registry(shell_timeout: int = 30) -> ToolRegistry:
    """创建包含默认三件套的工具注册表。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(ShellTool(timeout=shell_timeout))
    return registry