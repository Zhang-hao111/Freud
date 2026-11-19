"""工作区注册，各会话沿用 Session 中保存的工作目录。"""

import json
import uuid
from pathlib import Path

from agent.session import _write_json, _write_lock


class WorkspaceRegistry:
    def __init__(self, default_dir, path):
        self.default = {'id': 'default', 'title': '默认工作区',
                        'path': str(Path(default_dir).expanduser().resolve())}
        self.path = path

    def _projects(self):
        try:
            return json.loads(self.path.read_text(encoding='utf-8'))
        except FileNotFoundError:
            return []

    def list(self):
        return [self.default, *(row for row in self._projects() if row['path'] != self.default['path'])]

    def get(self, workspace_id):
        return next((row for row in self.list() if row['id'] == workspace_id), None)

    def session_directory(self, session):
        return Path(session.data.get('workspace') or self.default['path']).resolve()

    def add(self, path):
        if not isinstance(path, str) or not path.strip():
            raise ValueError('请输入项目目录的绝对路径')
        try:
            expanded = Path(path.strip()).expanduser()
            if not expanded.is_absolute():
                raise ValueError('请输入项目目录的绝对路径')
            directory = expanded.resolve(strict=True)
            if not directory.is_dir():
                raise ValueError('项目路径必须是已有目录')
        except (OSError, RuntimeError) as exc:
            raise ValueError('无法访问项目目录，请检查路径') from exc
        canonical = str(directory)
        with _write_lock(self.path.parent):
            projects = self._projects()
            for record in (self.default, *projects):
                if record['path'] == canonical:
                    return record
            record = {'id': uuid.uuid4().hex, 'title': directory.name or canonical, 'path': canonical}
            _write_json(self.path, [*projects, record])
            return record


def install_workspaces(runtime):
    runtime.provide('workspaces', WorkspaceRegistry(
        runtime.config['workspace'], runtime['sessions'].dir.parent / 'workspaces.json',
    ))
