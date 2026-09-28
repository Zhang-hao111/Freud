"""会话管理 — 每个会话独立保存记忆与消息历史，/resume 可恢复。

模型（参考 Claude Code）：
- 新会话从零开始，不注入任何历史记忆；
- 每个会话一个 JSON 文件，保存在 ~/.agent-harness/sessions/ 下，
  内容含：标题、时间戳、工作目录、take_note 记忆条目、完整消息历史；
- /resume 列出历史会话，选中后恢复其记忆与对话上下文。
"""

import json
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from agent.types import (
    Message, SystemMessage, UserMessage, AssistantMessage, ToolMessage, MemoryEntry,
)

SESSIONS_DIR = Path.home() / '.agent-harness' / 'sessions'
LEGACY_MEMORY = Path.home() / '.agent-harness' / 'memory.json'
DEFAULT_TITLE = '(未命名会话)'


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
        elif isinstance(m, AssistantMessage):
            out.append({
                'role': 'assistant',
                'content': m.content,
                'tool_calls': [_dump_tool_call(tc) for tc in (m.tool_calls or [])],
            })
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
            out.append(UserMessage(content=d.get('content', '')))
        elif role == 'assistant':
            out.append(AssistantMessage(content=d.get('content'), tool_calls=d.get('tool_calls') or []))
        elif role == 'tool':
            out.append(ToolMessage(content=d.get('content', ''), tool_call_id=d.get('tool_call_id', '')))
    return out


# ---- 会话 ----

class Session:
    """一个会话：独立的记忆条目 + 消息历史，落盘为一个 JSON 文件。"""

    def __init__(self, path: Path, data: dict | None = None, workspace: str = '.'):
        self.path = path
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

    def save(self, messages: list[Message] | None = None):
        if messages is not None:
            self.store_messages(messages)
        self.touch()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding='utf-8')
        os.replace(tmp, self.path)

    @classmethod
    def from_path(cls, path: Path) -> 'Session':
        data = json.loads(path.read_text(encoding='utf-8'))
        return cls(path, data=data)


# ---- 存储层 ----

class SessionStore:
    def __init__(self, sessions_dir: Path | None = None):
        # 默认值延迟到调用时解析，便于测试用临时目录替换
        self.dir = Path(sessions_dir) if sessions_dir else Path(SESSIONS_DIR)
        self._import_legacy()

    def new(self, workspace: str = '.') -> Session:
        session = Session(self.dir / f"session-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}.json",
                          workspace=workspace)
        session.save()
        return session

    def list(self) -> list[Session]:
        """按最近更新时间倒序返回全部会话。"""
        if not self.dir.exists():
            return []
        sessions = []
        for p in self.dir.glob('session-*.json'):
            try:
                sessions.append(Session.from_path(p))
            except (json.JSONDecodeError, OSError):
                continue
        sessions.sort(key=lambda s: s.data.get('updated_at', ''), reverse=True)
        return sessions

    def _import_legacy(self):
        """旧版全局 memory.json 非空且尚无会话时，导入为一个遗留会话。"""
        if self.dir.exists() and any(self.dir.glob('session-*.json')):
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
            self._dirty = False
        self.session.save(self.live_messages)
