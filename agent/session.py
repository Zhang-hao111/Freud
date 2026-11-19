"""会话管理 — 每个会话独立保存记忆与消息历史，/resume 可恢复。

模型（参考 Claude Code）：
- 新会话从零开始，不注入任何历史记忆；
- 每个会话一个 JSON 文件，保存在 ~/.agent-harness/sessions/ 下，
  内容含：标题、时间戳、工作目录、take_note 记忆条目、完整消息历史；
- /resume 列出历史会话，选中后恢复其记忆与对话上下文。
"""

import fcntl
import json
import os
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from agent.types import (
    Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage, MemoryEntry,
)

SESSIONS_DIR = Path.home() / '.agent-harness' / 'sessions'
LEGACY_MEMORY = Path.home() / '.agent-harness' / 'memory.json'
DEFAULT_TITLE = '(未命名会话)'


@contextmanager
def _write_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _turn_lock_path(directory, session_id):
    return directory / '.turn-locks' / f'session-{session_id}'


def _valid_session_id(session_id):
    return isinstance(session_id, str) and '/' not in session_id and '\\' not in session_id


def _write_json(path, data):
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f'.{path.name}.')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _cached_summary(path):
    try:
        record = json.loads(path.with_suffix('.meta').read_text(encoding='utf-8'))
        if (record['mtime'] == path.stat().st_mtime_ns
                and all(key in record for key in ('id', 'title', 'workspace', 'updated_at', 'revision', 'memory_count'))):
            return record
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


class SessionConflict(ValueError):
    pass


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def time_ago(iso: str, now: datetime | None = None) -> str:
    """把 ISO 时间戳转成'刚刚 / N 分钟前 / N 小时前 / N 天前 / N 个月前'。"""
    try:
        t = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return '未知时间'
    now = now or datetime.now(timezone.utc)
    delta = max(0, (now - t).total_seconds())
    if delta < 60:
        return '刚刚'
    if delta < 3600:
        return f'{int(delta // 60)} 分钟前'
    if delta < 86400:
        return f'{int(delta // 3600)} 小时前'
    days = int(delta // 86400)
    if days < 30:
        return f'{days} 天前'
    return f'{days // 30} 个月前'


# ---- 消息序列化 ----

def _dump_tool_call(tc) -> dict:
    if isinstance(tc, dict):
        return tc
    return {'id': tc.id, 'type': tc.type,
            'function': {'name': tc.function_name, 'arguments': tc.function_arguments}}


def dump_messages(messages: list[Message]) -> list[dict]:
    out = []
    for m in messages:
        if isinstance(m, SystemMessage):
            out.append({'role': 'system', 'content': m.content})
        elif isinstance(m, UserMessage):
            out.append({'role': 'user', 'content': m.content})
            if m.workflow:
                out[-1]['workflow'] = True
            if m.internal:
                out[-1]['internal'] = True
        elif isinstance(m, AssistantMessage):
            out.append({
                'role': 'assistant',
                'content': m.content,
                'tool_calls': [_dump_tool_call(tc) for tc in (m.tool_calls or [])],
            })
            if m.workflow:
                out[-1]['workflow'] = True
        elif isinstance(m, ToolMessage):
            out.append({'role': 'tool', 'content': m.content, 'tool_call_id': m.tool_call_id})
    return out


def load_messages(data: list[dict]) -> list[Message]:
    out: list[Message] = []
    for d in data or []:
        role = d.get('role')
        if role == 'system':
            out.append(SystemMessage(content=d.get('content', '')))
        elif role == 'user':
            out.append(UserMessage(content=d.get('content', ''), workflow=d.get('workflow', False),
                                   internal=d.get('internal', False)))
        elif role == 'assistant':
            out.append(AssistantMessage(content=d.get('content'), tool_calls=d.get('tool_calls') or [],
                                        workflow=d.get('workflow', False)))
        elif role == 'tool':
            out.append(ToolMessage(content=d.get('content', ''), tool_call_id=d.get('tool_call_id', '')))
    return out


# ---- 会话 ----

class Session:
    """一个会话：独立的记忆条目 + 消息历史，落盘为一个 JSON 文件。"""

    def __init__(self, path: Path, data: dict | None = None, workspace: str = '.'):
        self.path = path
        self._persisted = data is not None
        if data is None:
            data = {
                'version': 1,
                'id': f"{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}",
                'title': DEFAULT_TITLE,
                'created_at': _now_iso(),
                'updated_at': _now_iso(),
                'workspace': workspace,
                'memory': [],   # [{key, value, created_at, updated_at}]
                'messages': [],  # dump_messages 格式
            }
        self.data = data

    # ---- 标题 ----

    def note_title(self, text: str):
        """首个用户输入作为会话标题（后续不覆盖）。"""
        if self.data.get('title') == DEFAULT_TITLE and text:
            self.data['title'] = text.strip()[:40] or DEFAULT_TITLE
        self.touch()

    def touch(self):
        self.data['updated_at'] = _now_iso()

    # ---- 记忆 ----

    @property
    def memory(self) -> list[dict]:
        return self.data['memory']

    def set_memory(self, entries: list[dict]):
        self.data['memory'] = entries

    # ---- 消息 ----

    @property
    def messages(self) -> list[Message]:
        return load_messages(self.data.get('messages'))

    def store_messages(self, messages: list[Message]):
        self.data['messages'] = dump_messages(messages)

    # ---- 持久化 ----

    @property
    def metadata_path(self):
        return self.path.with_suffix('.meta')

    @property
    def artifacts_dir(self):
        return self.path.with_suffix('.artifacts')

    def summary(self):
        return {**{key: self.data.get(key) for key in ('id', 'title', 'workspace', 'updated_at')},
                'memory_count': len(self.data.get('memory') or [])}

    def _cache_summary(self):
        try:
            _write_json(self.metadata_path, {**self.summary(), 'revision': self.data.get('revision', 0),
                                             'mtime': self.path.stat().st_mtime_ns})
        except OSError:
            # 元数据只是缓存，下次读取可回退到原始会话。
            pass

    def _check_revision(self):
        if self.path.exists():
            current = _cached_summary(self.path)
            if current is None:
                current = self.from_path(self.path).data
            if self._persisted and current.get('revision', 0) == self.data.get('revision', 0):
                return
        elif not self._persisted:
            return
        raise SessionConflict('会话已被另一进程更新或删除，请重新打开后重试。')

    @contextmanager
    def turn(self):
        """让 CLI 和 Web 在同一会话内串行执行和保存回合。"""
        lock = _turn_lock_path(self.path.parent, self.data['id'])
        lock.parent.mkdir(mode=0o700, exist_ok=True)
        # 会话 JSON 会原子替换。锁文件单独存放，不删除，以免并发打开不同的 inode。
        fd = os.open(lock, os.O_CREAT | os.O_RDONLY, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SessionConflict('该会话正在运行，请等待本轮结束。') from exc
            try:
                self.data = self.from_path(self.path).data
            except FileNotFoundError as exc:
                raise SessionConflict('会话已被删除，请新建或恢复其他会话。') from exc
            yield
        finally:
            os.close(fd)

    def save(self, messages: list[Message] | None = None):
        with _write_lock(self.path.parent):
            self._check_revision()
            revision = self.data.get('revision', 0) + 1
            if messages is not None:
                self.store_messages(messages)
            self.touch()
            _write_json(self.path, {**self.data, 'revision': revision})
            self.data['revision'] = revision
            self._persisted = True
            self._cache_summary()

    def save_json_artifact(self, name, data):
        if Path(name).name != name or not name.endswith('.json'):
            raise ValueError('会话产物文件名无效')
        with _write_lock(self.path.parent):
            self._check_revision()
            self.artifacts_dir.mkdir(mode=0o700, exist_ok=True)
            _write_json(self.artifacts_dir / name, data)

    @classmethod
    def from_path(cls, path: Path) -> 'Session':
        data = json.loads(path.read_text(encoding='utf-8'))
        if not isinstance(data, dict) or not _valid_session_id(data.get('id')):
            raise ValueError('会话文件格式无效')
        return cls(path, data=data)


# ---- 存储层 ----

class SessionStore:
    def __init__(self, sessions_dir: Path | None = None):
        # 默认值延迟到调用时解析，便于测试用临时目录替换
        self.dir = Path(sessions_dir) if sessions_dir else Path(SESSIONS_DIR)
        self._import_legacy()

    def new(self, workspace: str = '.') -> Session:
        session = Session(self.dir, workspace=workspace)
        session.path = self.dir / f"session-{session.data['id']}.json"
        session.save()
        return session

    def has_active_turn(self, session_id):
        if not _valid_session_id(session_id):
            return False
        try:
            fd = os.open(_turn_lock_path(self.dir, session_id), os.O_RDONLY)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            return False
        except BlockingIOError:
            return True
        finally:
            os.close(fd)

    def delete(self, session: Session):
        with session.turn(), _write_lock(self.dir):
            if session.path.name.startswith('session-legacy-'):
                (self.dir / '.legacy-imported').touch()
            session.path.unlink()
            session.metadata_path.unlink(missing_ok=True)
            if session.artifacts_dir.is_dir():
                shutil.rmtree(session.artifacts_dir)

    def summaries(self):
        return sorted((row for _, row in self._summary_rows()), key=lambda row: row['updated_at'] or '', reverse=True)

    def _summary_rows(self):
        for path in self.dir.glob('session-*.json'):
            summary = _cached_summary(path)
            if summary is None:
                try:
                    with _write_lock(self.dir):
                        session = Session.from_path(path)
                        summary = session.summary()
                        session._cache_summary()
                except (OSError, ValueError):
                    continue
            yield path, summary

    def get(self, session_id):
        if not _valid_session_id(session_id):
            return None
        try:
            session = Session.from_path(self.dir / f'session-{session_id}.json')
            if session.data['id'] == session_id:
                return session
        except (OSError, ValueError):
            pass
        for path, summary in self._summary_rows():
            if summary['id'] == session_id:
                try:
                    return Session.from_path(path)
                except (OSError, ValueError):
                    return None
        return None

    def list(self) -> list[Session]:
        """按最近更新时间倒序返回全部会话。"""
        if not self.dir.exists():
            return []
        sessions = []
        for p in self.dir.glob('session-*.json'):
            try:
                sessions.append(Session.from_path(p))
            except (ValueError, OSError):
                continue
        sessions.sort(key=lambda s: s.data.get('updated_at', ''), reverse=True)
        return sessions

    def _import_legacy(self):
        """旧版全局 memory.json 非空且尚无会话时，导入为一个遗留会话。"""
        if (self.dir / '.legacy-imported').exists() or (self.dir.exists() and any(self.dir.glob('session-*.json'))):
            return
        if not LEGACY_MEMORY.exists():
            return
        try:
            entries = json.loads(LEGACY_MEMORY.read_text(encoding='utf-8'))
        except (json.JSONDecodeError, OSError):
            return
        if not entries:
            return
        session = Session(self.dir / f"session-legacy-{time.strftime('%Y%m%d-%H%M%S')}.json")
        session.data['title'] = '旧版全局记忆（自动导入）'
        session.data['memory'] = entries if isinstance(entries, list) else []
        session.save()


# ---- FileMemory 兼容接口 ----

class SessionMemory:
    """与 FileMemory 同接口（write/read/items/consolidate），底层存在会话文件里。

    传入 live_messages 以便每次落盘时一并保存当前消息历史。
    """

    def __init__(self, session: Session, live_messages: list[Message] | None = None):
        self.session = session
        self.live_messages = live_messages
        self._entries: dict[str, dict] = {
            e['key']: dict(e) for e in session.memory if e.get('key')
        }
        self._dirty = False

    def read(self, key: str) -> str | None:
        e = self._entries.get(key)
        return e['value'] if e else None

    def items(self) -> list[MemoryEntry]:
        return [MemoryEntry(key=e['key'], value=e.get('value', ''),
                            created_at=e.get('created_at', ''), updated_at=e.get('updated_at', ''))
                for e in self._entries.values()]

    def write(self, key: str, value: str):
        now = _now_iso()
        if key in self._entries:
            self._entries[key]['value'] = value
            self._entries[key]['updated_at'] = now
        else:
            self._entries[key] = {'key': key, 'value': value, 'created_at': now, 'updated_at': now}
        self._dirty = True

    def consolidate(self):
        """把记忆条目（连同当前消息历史）写回会话文件。"""
        if self._dirty:
            self.session.set_memory(list(self._entries.values()))
        self.session.save(self.live_messages)
        self._dirty = False
