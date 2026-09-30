"""工具系统 — read_file / write_file / shell 以及 ToolRegistry。"""

import fnmatch
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


class EditFileTool(BaseTool):
    name = 'edit_file'
    description = '对已有文件做精确字符串替换编辑，适合改动文件局部而不重写全文'
    parameters = {
        'type': 'object',
        'properties': {
            'path': {'type': 'string', 'description': '文件路径（绝对或相对），必须是已存在的文件'},
            'old_string': {'type': 'string', 'description': '要替换的原文，必须与文件内容逐字符一致（含缩进与换行）'},
            'new_string': {'type': 'string', 'description': '替换后的内容'},
            'replace_all': {'type': 'boolean', 'description': '为 true 时替换全部匹配；默认 false，要求 old_string 在文件中唯一'},
        },
        'required': ['path', 'old_string', 'new_string'],
    }

    def execute(self, args: dict) -> ToolResult:
        raw = args.get('path', '')
        old_string = args.get('old_string')
        new_string = args.get('new_string')
        if not isinstance(raw, str) or not raw.strip():
            return ToolResult(success=False, error='缺少 path 参数')
        if not isinstance(old_string, str) or not old_string:
            return ToolResult(success=False, error='old_string 不能为空（新建文件请用 write_file）')
        if not isinstance(new_string, str):
            return ToolResult(success=False, error='new_string 必须是字符串')
        if old_string == new_string:
            return ToolResult(success=False, error='old_string 与 new_string 相同，无需编辑')
        file_path = os.path.abspath(raw)
        if not os.path.isfile(file_path):
            return ToolResult(success=False, error=f'文件不存在: {file_path}')
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
        except UnicodeDecodeError:
            return ToolResult(success=False, error=f'文件不是 UTF-8 编码，无法编辑: {file_path}')
        except Exception as e:
            return ToolResult(success=False, error=f'读取失败: {e}')
        count = content.count(old_string)
        if count == 0:
            return ToolResult(success=False, error='未找到 old_string，请先 read_file 核对原文（注意空格、缩进与换行）')
        replace_all = bool(args.get('replace_all'))
        if count > 1 and not replace_all:
            return ToolResult(success=False, error=f'old_string 出现 {count} 次，请提供更长的上下文使其唯一，或设 replace_all=true')
        new_content = content.replace(old_string, new_string) if replace_all else content.replace(old_string, new_string, 1)
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
        except Exception as e:
            return ToolResult(success=False, error=f'写入失败: {e}')
        replaced = count if replace_all else 1
        return ToolResult(success=True, data=f'已替换 {replaced} 处: {file_path}')


GREP_SKIP_DIRS = {'.git', '.hg', '.svn', 'node_modules', '__pycache__', '.venv', 'venv',
                  '.pytest_cache', '.mypy_cache', 'dist', 'build'}
GREP_MAX_FILE_SIZE = 10 * 1024 * 1024  # 跳过超过 10MB 的文件，避免在大数据集上卡死


class GrepTool(BaseTool):
    name = 'grep'
    description = '用正则表达式搜索文件内容，返回 file:line:text 形式的匹配列表；目录会递归搜索'
    parameters = {
        'type': 'object',
        'properties': {
            'pattern': {'type': 'string', 'description': '正则表达式'},
            'path': {'type': 'string', 'description': '文件或目录路径，默认当前目录'},
            'include': {'type': 'string', 'description': '文件名过滤，如 *.py，默认所有文件'},
            'ignore_case': {'type': 'boolean', 'description': '是否忽略大小写，默认 false'},
            'max_results': {'type': 'integer', 'description': '最多返回的匹配行数，默认 200'},
        },
        'required': ['pattern'],
    }

    def execute(self, args: dict) -> ToolResult:
        pattern = args.get('pattern')
        if not isinstance(pattern, str) or not pattern:
            return ToolResult(success=False, error='缺少 pattern 参数')
        raw = args.get('path', '.') if isinstance(args.get('path'), str) else '.'
        base = os.path.abspath(raw)
        if os.path.isfile(base):
            files = [base]
        elif os.path.isdir(base):
            files = []
            for root, dirs, names in os.walk(base):
                dirs[:] = [d for d in dirs if d not in GREP_SKIP_DIRS]
                files.extend(os.path.join(root, name) for name in names)
        else:
            return ToolResult(success=False, error=f'路径不存在: {base}')
        include = args.get('include') if isinstance(args.get('include'), str) else '*'
        try:
            regex = re.compile(pattern, re.IGNORECASE if args.get('ignore_case') else 0)
        except re.error as e:
            return ToolResult(success=False, error=f'无效的正则表达式: {e}')
        try:
            limit = min(max(int(args.get('max_results') or 200), 1), 1000)
        except (TypeError, ValueError):
            limit = 200
        matches = []
        for file_path in files:
            if not fnmatch.fnmatch(os.path.basename(file_path), include):
                continue
            try:
                if os.path.getsize(file_path) > GREP_MAX_FILE_SIZE:
                    continue
                with open(file_path, 'rb') as probe:
                    if b'\0' in probe.read(8192):
                        continue  # 二进制文件
                with open(file_path, 'r', encoding='utf-8', errors='replace') as f:
                    for lineno, line in enumerate(f, 1):
                        if regex.search(line):
                            display = os.path.relpath(file_path, base)
                            if display.startswith('..'):
                                display = file_path
                            matches.append(f'{display}:{lineno}: {line.rstrip()[:500]}')
                            if len(matches) >= limit:
                                matches.append(f'...[已达 max_results={limit}，结果已截断]')
                                return ToolResult(success=True, data='\n'.join(matches))
            except OSError:
                continue
        if not matches:
            return ToolResult(success=True, data='未找到匹配')
        return ToolResult(success=True, data='\n'.join(matches))


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
    """创建包含默认工具的注册表。"""
    registry = ToolRegistry()
    registry.register(ReadFileTool())
    registry.register(WriteFileTool())
    registry.register(EditFileTool())
    registry.register(GrepTool())
    registry.register(ShellTool(timeout=shell_timeout))
    return registry