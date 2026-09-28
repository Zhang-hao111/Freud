"""会话管理测试：独立记忆、消息序列化、/resume 恢复、遗留导入（阶段五）。"""
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import session as session_mod
from agent.session import (
    Session, SessionStore, SessionMemory, dump_messages, load_messages, time_ago,
)
from agent.types import (
    AssistantMessage, SystemMessage, ToolMessage, UserMessage,
)
from agent.core import build_memory_block


class SessionRoundtripTest(unittest.TestCase):
    def test_new_session_starts_empty(self):
        """新会话记忆必须为空（不携带任何历史）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            s = store.new(workspace='/w')
            self.assertEqual(s.memory, [])
            self.assertEqual(build_memory_block(SessionMemory(s)), '')

    def test_memory_write_persists_to_session_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            s = store.new(workspace='/w')
            mem = SessionMemory(s, live_messages=s.messages)
            mem.write('偏好', '用 Python')
            mem.consolidate()

            loaded = Session.from_path(s.path)
            self.assertEqual(len(loaded.memory), 1)
            self.assertEqual(loaded.memory[0]['key'], '偏好')
            block = build_memory_block(SessionMemory(loaded))
            self.assertIn('偏好', block)
            self.assertIn('用 Python', block)

    def test_message_roundtrip(self):
        messages = [
            SystemMessage(content='sys-prompt'),
            UserMessage(content='你好'),
            AssistantMessage(content=None, tool_calls=[
                {'id': 't1', 'type': 'function',
                 'function': {'name': 'shell', 'arguments': '{"command":"ls"}'}},
            ]),
            ToolMessage(content='file1', tool_call_id='t1'),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            store = SessionStore(Path(tmp))
            s = store.new(workspace='/w')
            s.save(messages)

            loaded = Session.from_path(s.path)
            back = loaded.messages
            self.assertIsInstance(back[0], SystemMessage)
            self.assertEqual(back[0].content, 'sys-prompt')
            self.assertIsInstance(back[2], AssistantMessage)
            self.assertEqual(back[2].tool_calls[0]['id'], 't1')
            self.assertIsInstance(back[3], ToolMessage)
            self.assertEqual(back[3].tool_call_id, 't1')

    def test_list_sorted_by_updated_desc(self):
        with tempfile.TemporaryDirectory() as tmp:
            # 隔离真实的遗留 memory.json，避免自动导入干扰计数
            legacy = Path(tmp) / 'no-legacy.json'
            with mock.patch.object(session_mod, 'LEGACY_MEMORY', legacy):
                store = SessionStore(Path(tmp))
                old = store.new(workspace='/w')
                old.data['title'] = '旧会话'
                old.data['updated_at'] = '2026-01-01T00:00:00+00:00'
                old.save()
                new = store.new(workspace='/w')
                new.data['title'] = '新会话'
                new.save()

                listed = store.list()
                self.assertEqual([s.data['title'] for s in listed], ['新会话', '旧会话'])

    def test_note_title_only_first_wins(self):
        with tempfile.TemporaryDirectory() as tmp:
            s = SessionStore(Path(tmp)).new(workspace='/w')
            s.note_title('第一个标题')
            s.note_title('第二个标题')
            self.assertEqual(s.data['title'], '第一个标题')


class SessionMemoryCompatTest(unittest.TestCase):
    def test_filememory_interface(self):
        """SessionMemory 与 FileMemory 同接口：write/read/items/consolidate。"""
        with tempfile.TemporaryDirectory() as tmp:
            s = SessionStore(Path(tmp)).new(workspace='/w')
            mem = SessionMemory(s)
            mem.write('k', 'v1')
            self.assertEqual(mem.read('k'), 'v1')
            mem.write('k', 'v2')
            self.assertEqual(mem.read('k'), 'v2')
            mem.consolidate()
            items = mem.items()
            self.assertEqual(len(items), 1)
            self.assertEqual(items[0].key, 'k')
            self.assertEqual(items[0].value, 'v2')


class LegacyImportTest(unittest.TestCase):
    def test_legacy_memory_imported_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            legacy = Path(tmp) / 'legacy-memory.json'
            legacy.write_text(
                '[{"key":"task1","value":"two_sum","created_at":"t","updated_at":"t"}]',
                encoding='utf-8')
            sessions_dir = Path(tmp) / 'sessions'
            with mock.patch.object(session_mod, 'LEGACY_MEMORY', legacy):
                store = SessionStore(sessions_dir)
                listed = store.list()
                self.assertEqual(len(listed), 1)
                self.assertIn('旧版全局记忆', listed[0].data['title'])
                self.assertEqual(listed[0].memory[0]['key'], 'task1')

                # 已有会话时不再重复导入
                store2 = SessionStore(sessions_dir)
                self.assertEqual(len(store2.list()), 1)


class TimeAgoTest(unittest.TestCase):
    def test_buckets(self):
        now = datetime(2026, 9, 28, 12, 0, 0, tzinfo=timezone.utc)

        def ago(**kw):
            return (now - timedelta(**kw)).isoformat()

        self.assertEqual(time_ago(ago(seconds=10), now=now), '刚刚')
        self.assertEqual(time_ago(ago(minutes=5), now=now), '5 分钟前')
        self.assertEqual(time_ago(ago(hours=3), now=now), '3 小时前')
        self.assertEqual(time_ago(ago(days=2), now=now), '2 天前')
        self.assertEqual(time_ago(ago(days=45), now=now), '1 个月前')


class ResumeIntentTest(unittest.TestCase):
    def test_slash_resume(self):
        from agent.cli import _parse_intent
        self.assertEqual(_parse_intent('/resume'), {'type': 'resume'})
        self.assertEqual(_parse_intent('/sessions'), {'type': 'resume'})

    def test_natural_language_resume(self):
        from agent.cli import _parse_intent
        for text in ('恢复会话', '帮我找回会话', '继续上次的任务'):
            self.assertEqual(_parse_intent(text)['type'], 'resume')


if __name__ == '__main__':
    unittest.main()
