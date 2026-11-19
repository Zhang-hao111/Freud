"""工作区文件访问和 Shell 进程隔离。"""

import os
import re
import shutil
import stat
import subprocess
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def workspace_file(workspace, raw, *, write=False):
    root = Path(workspace).resolve()
    path = Path(os.path.abspath(root / raw))
    if not path.is_relative_to(root) or path == root:
        raise ValueError('文件路径必须位于当前工作区内')
    parts = path.relative_to(root).parts
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    directory = os.open(root, dir_flags)
    try:
        for part in parts[:-1]:
            if write:
                try:
                    os.mkdir(part, dir_fd=directory)
                except FileExistsError:
                    pass
            child = os.open(part, dir_flags, dir_fd=directory)
            os.close(directory)
            directory = child
        flags = os.O_WRONLY | os.O_CREAT if write else os.O_RDONLY
        fd = os.open(parts[-1], flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=directory)
        with os.fdopen(fd, 'w' if write else 'r', encoding='utf-8') as file:
            metadata = os.fstat(file.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ValueError('仅支持工作区内的普通文件，不允许硬链接')
            if write:
                os.ftruncate(file.fileno(), 0)
            yield file
    finally:
        os.close(directory)


def sandbox_command(workspace, command):
    bwrap = shutil.which('bwrap')
    if not bwrap:
        raise ValueError('Shell 工作区隔离需要 Bubblewrap 0.8.0 或更新版本（bwrap）')
    root = str(Path(workspace).resolve())
    if root == '/' or root in ('/usr', '/bin', '/lib', '/lib64', '/proc', '/dev', '/etc', '/tmp'):
        raise ValueError('请选择具体项目目录作为 Shell 工作区')
    try:
        version = subprocess.run([bwrap, '--version'], capture_output=True, text=True,
                                 check=True, timeout=2).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError('无法读取 Bubblewrap 版本，请检查 bwrap 安装') from exc
    detected = re.search(r'\bbubblewrap (\d+)\.(\d+)\.(\d+)\b', version)
    if not detected:
        raise ValueError('无法识别 Bubblewrap 版本，请检查 bwrap 安装')
    if tuple(map(int, detected.groups())) < (0, 8, 0):
        current = '.'.join(detected.groups())
        raise ValueError(f'Shell 工作区隔离需要 Bubblewrap 0.8.0 或更新版本，当前版本为 {current}')
    args = [bwrap, '--unshare-all', '--unshare-user', '--disable-userns', '--die-with-parent',
            '--clearenv', '--setenv', 'PATH', '/usr/local/bin:/usr/bin:/bin',
            '--setenv', 'LANG', 'C.UTF-8', '--setenv', 'HOME', '/tmp',
            '--setenv', 'TMPDIR', '/tmp', '--proc', '/proc', '--dev', '/dev',
            '--size', '67108864', '--tmpfs', '/tmp']
    for path in ('/usr', '/bin', '/lib', '/lib64', '/etc/alternatives', '/etc/ld.so.cache'):
        if os.path.islink(path):
            args += ['--symlink', os.readlink(path), path]
        elif os.path.exists(path):
            args += ['--ro-bind', path, path]
    args += ['--bind', root, root, '--chdir', root, '--', '/bin/sh', '-c', command]
    return args
