import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from agent.llm import MockLLM
from agent.session import SessionStore
from agent.types import Action, AssistantMessage, ToolMessage, ToolResult, UserMessage
from agent.web import WebAgent


class WebAgentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'workspace': self.tmp.name, 'traces_dir': str(Path(self.tmp.name) / 'traces'), 'shell_timeout': 5, 'max_steps': 4, 'api_key': '', 'model': '', 'api_base': ''}
        self.store = SessionStore(Path(self.tmp.name) / 'sessions')

    def test_rejected_write_is_fed_back_and_conversation_can_continue(self):
        target = Path(self.tmp.name) / 'example.txt'
        action = Action(type='call_tool', tool='write_file', args={'path': str(target), 'content': 'hello'}, tool_call_id='call_1')
        llm = MockLLM([action, '收到'])
        app = WebAgent(self.config, self.store, lambda: llm)
        session = self.store.new(workspace=self.tmp.name)
        events = []
        def emit(event):
            events.append(event)
            if event['type'] == 'approval':
                self.assertTrue(app.approve(event['id'], False))
        app.chat(session, '测试', emit)
        self.assertFalse(target.exists())
        messages = app.conversations.session(session.data['id']).messages
        calls = [m for m in messages if isinstance(m, AssistantMessage) and m.tool_calls]
        results = [m for m in messages if isinstance(m, ToolMessage)]
        self.assertEqual(calls[0].tool_calls[0]['id'], results[0].tool_call_id)
        self.assertIn('拒绝', results[0].content)
        self.assertEqual(messages[-1].content, '收到')
        self.assertEqual([e['type'] for e in events], ['tool', 'approval', 'result', 'assistant'])

    def test_approved_write_uses_real_tool(self):
        target = Path(self.tmp.name) / 'approved.txt'
        action = Action(type='call_tool', tool='write_file', args={'path': str(target), 'content': 'hello'}, tool_call_id='call_1')
        app = WebAgent(self.config, self.store, lambda: MockLLM([action, '完成']))
        session = self.store.new(workspace=self.tmp.name)
        def emit(event):
            if event['type'] == 'approval':
                app.approve(event['id'], True)
        app.chat(session, '写入', emit)
        self.assertEqual(target.read_text(), 'hello')
        self.assertEqual(app.conversations.session(session.data['id']).messages[-1].content, '完成')

    def test_accept_mode_writes_without_approval(self):
        target = Path(self.tmp.name) / 'accepted.txt'
        action = Action(type='call_tool', tool='write_file', args={'path': str(target), 'content': 'ok'}, tool_call_id='call_1')
        app = WebAgent(self.config, self.store, lambda: MockLLM([action, '完成']))
        session = self.store.new(workspace=self.tmp.name)
        events = []
        app.chat(session, '写入', events.append, permission_mode='accept')
        self.assertEqual(target.read_text(), 'ok')
        self.assertFalse(any(event['type'] == 'approval' for event in events))

    def test_accept_asks_for_escalated_command_and_yolo_skips_it(self):
        for mode, expected_approval in [('accept', True), ('yolo', False)]:
            action = Action(type='call_tool', tool='shell', args={'command': 'fdisk -l'}, tool_call_id='call_1')
            app = WebAgent(self.config, self.store, lambda: MockLLM([action, '完成']))
            session = self.store.new(workspace=self.tmp.name)
            events = []
            def emit(event):
                events.append(event)
                if event['type'] == 'approval':
                    app.approve(event['id'], True)
            with patch('agent.registry.ToolRegistry.execute', return_value=ToolResult(True, data='ok')) as execute:
                app.chat(session, '检查', emit, permission_mode=mode)
            self.assertEqual(any(event['type'] == 'approval' for event in events), expected_approval)
            execute.assert_called_once()

    def test_yolo_still_blocks_denied_commands(self):
        action = Action(type='call_tool', tool='shell', args={'command': ':(){ :|:& };:'}, tool_call_id='call_1')
        app = WebAgent(self.config, self.store, lambda: MockLLM([action, '完成']))
        session = self.store.new(workspace=self.tmp.name)
        events = []
        app.chat(session, '执行', events.append, permission_mode='yolo')
        self.assertTrue(any(event['type'] == 'result' and '安全护栏' in event['content'] for event in events))
        self.assertFalse(any(event['type'] == 'approval' for event in events))

    def test_invalid_permission_mode_is_rejected(self):
        app = WebAgent(self.config, self.store, mock=True)
        session = self.store.new(workspace=self.tmp.name)
        with self.assertRaises(ValueError):
            app.chat(session, '执行', lambda event: None, permission_mode='invalid')

    def test_mock_approval_does_not_write(self):
        app = WebAgent(self.config, self.store, mock=True)
        session = self.store.new(workspace=self.tmp.name)
        events = []
        def emit(event):
            events.append(event)
            if event['type'] == 'approval':
                app.approve(event['id'], True)
        app.chat(session, '测试', emit)
        self.assertIn('未执行', next(e['content'] for e in events if e['type'] == 'result'))
        self.assertFalse(Path('mock.txt').exists())

    def test_disconnect_preserves_tool_protocol(self):
        action = Action(type='call_tool', tool='write_file', args={'path': 'a', 'content': 'x'}, tool_call_id='call_1')
        app = WebAgent(self.config, self.store, lambda: MockLLM([action]))
        session = self.store.new(workspace=self.tmp.name)
        def emit(event):
            raise BrokenPipeError()
        with self.assertRaises(BrokenPipeError):
            app.chat(session, '测试', emit)
        messages = app.conversations.session(session.data['id']).messages
        self.assertEqual(messages[-1].tool_call_id, 'call_1')
        self.assertIn('中断', messages[-1].content)
        self.assertNotIn(session.data['id'], app.conversations.active)

    def test_disconnect_after_tool_execution_persists_actual_result(self):
        action = Action(type='call_tool', tool='shell', args={'command': 'pwd'}, tool_call_id='pwd')
        app = WebAgent(self.config, self.store, lambda: MockLLM([action]))
        session = app.conversations.new_session()

        def emit(event):
            if event['type'] in ('result', 'error'):
                raise BrokenPipeError()

        with self.assertRaises(BrokenPipeError):
            app.chat(session, '检查当前目录', emit)
        restored = app.conversations.session(session.data['id'])
        self.assertEqual(restored.messages[-1].tool_call_id, 'pwd')
        self.assertEqual(restored.messages[-1].content.strip(), self.tmp.name)

    def test_new_turn_uses_latest_history_even_if_transport_holds_an_old_snapshot(self):
        conversations = WebAgent(self.config, self.store, lambda: MockLLM(['完成'])).conversations
        session = conversations.new_session()
        stale = conversations.session(session.data['id'])
        conversations.chat(session, '第一轮', lambda _event: None)
        conversations.chat(stale, '第二轮', lambda _event: None)

        restored = conversations.session(session.data['id'])
        self.assertEqual([message.content for message in restored.messages if isinstance(message, UserMessage)],
                         ['第一轮', '第二轮'])

    def test_duplicate_run_is_rejected(self):
        app = WebAgent(self.config, self.store, mock=True)
        session = self.store.new(workspace=self.tmp.name)
        app.conversations.active.add(session.data['id'])
        with self.assertRaises(ValueError):
            app.chat(session, '测试', lambda event: None)

    def test_workspace_persists_and_concurrent_tools_use_each_session_directory(self):
        root = Path(self.tmp.name)
        project = root / 'project'
        project.mkdir()
        alias = root / 'alias'
        alias.symlink_to(project, target_is_directory=True)

        def model():
            return MockLLM([
                Action(type='call_tool', tool='write_file',
                       args={'path': 'relative.txt', 'content': '项目文件'}, tool_call_id='write'),
                Action(type='call_tool', tool='read_file', args={'path': 'relative.txt'}, tool_call_id='read'),
                Action(type='call_tool', tool='shell', args={'command': 'pwd'}, tool_call_id='pwd'),
                '完成',
            ])

        app = WebAgent(self.config, self.store, model)
        with self.assertRaisesRegex(ValueError, '绝对路径'):
            app.conversations.workspaces.add('relative')
        added = app.conversations.workspaces.add(str(project))
        self.assertEqual(app.conversations.workspaces.add(str(alias)), added)
        app = WebAgent(self.config, self.store, model)
        self.assertEqual(app.conversations.workspaces.list(), [app.conversations.workspaces.default, added])
        default_session = app.conversations.new_session()
        project_session = app.conversations.new_session(added['id'])
        sessions = (default_session, project_session)
        directories = (root, project)
        ids = app.conversations.workspace_ids()
        self.assertEqual([app.conversations.session_info(s, ids)['workspace_id'] for s in sessions],
                         ['default', added['id']])

        def run(session):
            events = []
            app.chat(session, '验证工作目录', events.append, permission_mode='accept')
            return [event['content'] for event in events if event['type'] == 'result']

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(run, sessions))
        for directory, result in zip(directories, results):
            self.assertTrue((directory / 'relative.txt').is_file())
            self.assertEqual(result[1:], ['项目文件', str(directory) + '\n'])
        self.assertEqual(len(list((root / 'traces').glob('trace-*.json'))), 2)


if __name__ == '__main__':
    unittest.main()
