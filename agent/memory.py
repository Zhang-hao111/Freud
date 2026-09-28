"""FileMemory — 文件级持久化 key-value 存储，跨会话保持上下文。"""

import json
import os
from datetime import datetime, timezone
from agent.types import MemoryEntry


class FileMemory:
    """跨会话持久化 key-value 存储，按需检索而非全量载入。"""

    def __init__(self, file_path: str):
        self.file_path = file_path
        self._entries: dict[str, MemoryEntry] = {}
        self._dirty = False
        self._load()

    def _load(self):
        """从磁盘加载所有条目。"""
        try:
            with open(self.file_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if isinstance(data, list):
                for item in data:
                    entry = MemoryEntry(**item)
                    self._entries[entry.key] = entry
        except (FileNotFoundError, json.JSONDecodeError, TypeError):
            self._entries = {}

    def read(self, key: str) -> str | None:
        entry = self._entries.get(key)
        return entry.value if entry else None

    def items(self) -> list[MemoryEntry]:
        """返回全部条目快照（供上下文检索）。"""
        return list(self._entries.values())

    def write(self, key: str, value: str):
        now = datetime.now(timezone.utc).isoformat()
        if key in self._entries:
            self._entries[key].value = value
            self._entries[key].updated_at = now
        else:
            self._entries[key] = MemoryEntry(
                key=key, value=value,
                created_at=now, updated_at=now,
            )
        self._dirty = True

    def consolidate(self):
        """将脏数据写回磁盘。"""
        if not self._dirty:
            return
        parent = os.path.dirname(self.file_path)
        if parent:  # 裸文件名（无目录前缀）时无需建目录
            os.makedirs(parent, exist_ok=True)
        data = [
            {
                'key': e.key,
                'value': e.value,
                'created_at': e.created_at,
                'updated_at': e.updated_at,
            }
            for e in self._entries.values()
        ]
        with open(self.file_path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        self._dirty = False