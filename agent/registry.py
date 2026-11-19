"""工具系统 — read_file / write_file / shell 以及 ToolRegistry。"""

import codecs
import os
import selectors
import signal
import subprocess
import time

from agent.execution_scope import sandbox_command, workspace_file
from agent.types import BaseTool, ToolResult


# 单个工具返回给 LLM 的最大字符数，防止一次 cat 大文件撑爆上下文
MAX_TOOL_OUTPUT = 20000


def _clip(text: str) -> str:
    """过长的工具输出截断并附加说明。"""
    if not text or len(text) <= MAX_TOOL_OUTPUT:
        return text
    return text[:MAX_TOOL_OUTPUT] + '\n...[输出过长已截断]'


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
 
    def __init__(self, workspace: str | None = None):
        self.workspace = workspace

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('path', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 path 参数')
        file_path = os.path.abspath(os.path.join(self.workspace or '', raw))
        try:
            source = workspace_file(self.workspace, raw) if self.workspace else open(file_path, encoding='utf-8')
            with source as f:
                content = f.read(MAX_TOOL_OUTPUT + 1)
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

    def __init__(self, workspace: str | None = None):
        self.workspace = workspace

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('path', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 path 参数')
        content = args.get('content')
        if not isinstance(content, str):
            return ToolResult(success=False, error='content 参数必须是字符串')
        file_path = os.path.abspath(os.path.join(self.workspace or '', raw))
        try:
            if not self.workspace:
                os.makedirs(os.path.dirname(file_path), exist_ok=True)
            target = workspace_file(self.workspace, raw, write=True) if self.workspace else open(file_path, 'w', encoding='utf-8')
            with target as f:
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

    def __init__(self, timeout: int = 30, workspace: str | None = None, checkpoint=None):
        self.timeout = timeout
        self.workspace = workspace
        self.checkpoint = checkpoint

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('command', '')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 command 参数')
        try:
            argv = sandbox_command(self.workspace, raw) if self.workspace else ['/bin/sh', '-c', raw]
            output, code = self._run(argv)
            if code == 0:
                return ToolResult(success=True, data=output)
            return ToolResult(success=False, data=output, error=f'返回码 {code}')
        except subprocess.TimeoutExpired:
            return ToolResult(success=False, error=f'命令执行超时 ({self.timeout}s)')
        except (OSError, ValueError) as e:
            return ToolResult(success=False, error=f'执行失败: {e}')

    def _run(self, argv):
        deadline = time.monotonic() + self.timeout
        decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        marker = '\n...[输出过长，中间已截断]...\n'
        tail_chars = MAX_TOOL_OUTPUT // 5
        head_chars = MAX_TOOL_OUTPUT - tail_chars - len(marker)
        output = ''
        with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                              start_new_session=True) as process, selectors.DefaultSelector() as selector:
            try:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map() or process.poll() is None:
                    if self.checkpoint:
                        self.checkpoint()
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, self.timeout)
                    if not selector.get_map():
                        time.sleep(min(remaining, .1))
                        continue
                    for key, _ in selector.select(min(remaining, .1)):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output += decoder.decode(chunk, final=not chunk)
                        if len(output) > MAX_TOOL_OUTPUT:
                            output = output[:head_chars] + marker + output[-tail_chars:]
                return output, process.returncode
            except BaseException:
                # 终止管道内可能继续持有 stdout 的子进程。
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                raise


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


def create_default_registry(shell_timeout: int = 30, *, workspace: str | None = None, checkpoint=None) -> ToolRegistry:
    """创建包含默认三件套的工具注册表。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool(workspace))
    registry.register(WriteFileTool(workspace))
    registry.register(ShellTool(timeout=shell_timeout, workspace=workspace, checkpoint=checkpoint))
    return registry
