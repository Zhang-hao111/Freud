import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from agent import cli, config
from agent.context import CONTEXT_CHARS, model_context
from agent.core import build_chat_system_prompt, run_chat_turn
from agent.llm import MockLLM, parse_tool_call
from agent.registry import MAX_TOOL_OUTPUT, ShellTool, create_default_registry
from agent.session import SessionConflict, SessionMemory, SessionStore, dump_messages, load_messages
from agent.tool_execution import execute_tool
from agent.types import Action, AssistantMessage, LLMResponse, SystemMessage, ToolCall, ToolMessage, UserMessage
from agent.web import WebAgent
from agent.workflows import WorkflowReply, WorkflowTask


class ContextTests(unittest.TestCase):
    def test_route_mentions_do_not_remove_user_goal_or_tool_request(self):
        call = ToolCall(id='read', function_name='read_file', function_arguments='{}')
        messages = [
            UserMessage(content='检查工作流接口'),
            AssistantMessage(content='我会检查 `/api/workflows/` 的路由。', tool_calls=[call]),
            ToolMessage(content='fixture route', tool_call_id='read'),
        ]
        self.assertEqual(model_context(messages), messages)

    def test_legacy_workflow_report_remains_outside_model_context(self):
        report = '[下载训练报告](/api/workflows/session/training-report/' + 'f' * 32 + ')'
        goal = UserMessage(content='现在检查工作流接口')
        messages = [UserMessage(content='训练模型'), AssistantMessage(content=report), goal]
        self.assertEqual(model_context(messages), [goal])

    def test_long_batch_keeps_goal_and_acceptance_criteria_after_internal_feedback(self):
        goal = 'original goal: ' + 'x' * 17000 + ' acceptance criteria'
        messages = [SystemMessage(content='system'), UserMessage(content=goal)]
        for number in range(8):
            call_id = f'tool-{number}'
            messages.extend([
                AssistantMessage(tool_calls=[ToolCall(id=call_id, function_name='shell')]),
                ToolMessage(content='stderr' + '!' * 9000, tool_call_id=call_id),
                UserMessage(content='internal feedback', internal=True),
            ])
        restored = load_messages(dump_messages(messages))
        projected = model_context(restored)
        self.assertEqual(projected[1].content, goal)
        self.assertTrue(projected[-1].internal)
        self.assertLess(sum(len(m.content or '') for m in projected), CONTEXT_CHARS)

    def test_oversized_latest_goal_fails_explicitly_instead_of_truncating(self):
        with self.assertRaisesRegex(ValueError, '超过模型上下文预算'):
            model_context([UserMessage(content='a' * CONTEXT_CHARS)])

    def test_latest_tool_batch_keeps_each_receipt_and_diagnostic_tail(self):
        goal = UserMessage(content='检查这批工具结果')
        calls = [ToolCall(id=str(i), function_name='read_file') for i in range(6)]
        action = AssistantMessage(tool_calls=calls)
        replies = [ToolMessage(content='开始' + 'x' * 9000 + '最终错误', tool_call_id=call.id) for call in calls]
        messages = [goal, action, *replies]

        projected = model_context(messages)

        self.assertEqual(projected[:2], [goal, action])
        self.assertEqual([reply.tool_call_id for reply in projected[2:]], [call.id for call in calls])
        self.assertTrue(all(reply.content.startswith('开始') and reply.content.endswith('最终错误')
                            for reply in projected[2:]))
        self.assertLess(sum(len(m.content or '') for m in projected) + len(str(calls)), CONTEXT_CHARS)
        self.assertEqual(messages[2:], replies)
        self.assertGreater(len(replies[0].content), 9000)

    def test_oversized_tool_arguments_abort_instead_of_replaying_goal(self):
        call = ToolCall(id='write', function_name='write_file', function_arguments='x' * CONTEXT_CHARS)
        with self.assertRaisesRegex(ValueError, '工具调用.*上下文预算'):
            model_context([UserMessage(content='写入'),
                           AssistantMessage(tool_calls=[call]),
                           ToolMessage(content='已写入', tool_call_id=call.id)])

    def test_context_bounded_and_keeps_complete_tool_call(self):
        tool = ToolCall(id='current', function_name='shell', function_arguments='{}')
        messages = [SystemMessage(content='system')]
        messages.extend(UserMessage(content='old' + 'x' * 9000) for _ in range(15))
        messages.extend([
            UserMessage(content='task question', workflow=True),
            AssistantMessage(content='task result', workflow=True),
            UserMessage(content='current request'),
            AssistantMessage(tool_calls=[tool]),
            ToolMessage(content='partial output', tool_call_id='current'),
        ])
        projected = model_context(messages)
        self.assertLess(sum(len(m.content or '') for m in projected), CONTEXT_CHARS)
        self.assertNotIn('task result', [m.content for m in projected])
        self.assertEqual([m.content for m in projected[-3:]], ['current request', None, 'partial output'])
        self.assertEqual(projected[-1].tool_call_id, projected[-2].tool_calls[0].id)
        self.assertEqual(len(messages), 21)

    def test_new_note_is_projected_without_changing_saved_system_prompt(self):
        with tempfile.TemporaryDirectory() as root:
            session = SessionStore(Path(root)).new()
            messages = [SystemMessage(content=build_chat_system_prompt(root)), UserMessage(content='remember')]
            memory = SessionMemory(session, messages)
            memory.write('fixture-note', 'remember this')
            memory.consolidate()
            saved = SessionStore(Path(root)).get(session.data['id'])
            projected = model_context(saved.messages, SessionMemory(saved))
            self.assertIn('fixture-note', projected[1].content)
            self.assertNotIn('fixture-note', saved.messages[0].content)

    def test_bad_tool_arguments_get_protocol_receipt(self):
        call = ToolCall(id='malformed', function_name='take_note', function_arguments='[]')
        bad = LLMResponse(actions=[parse_tool_call(call)], message=AssistantMessage(tool_calls=[call]))
        llm = Mock()
        snapshots = []
        def respond(messages, _tools):
            snapshots.append(messages)
            return bad if len(snapshots) == 1 else LLMResponse(message=AssistantMessage(content='ok'))
        llm.chat.side_effect = respond
        handler = Mock()
        self.assertTrue(run_chat_turn([UserMessage(content='task')], llm, [], handler, Mock(), max_steps=2))
        handler.assert_not_called()
        self.assertEqual(snapshots[-1][-1].tool_call_id, 'malformed')
        self.assertIn('JSON 对象', snapshots[-1][-1].content)


class PersistenceTests(unittest.TestCase):
    def test_resumed_cli_refreshes_history_and_excludes_web_mutations_during_turn(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = SessionStore(root / 'sessions')
            settings = {'workspace': str(root), 'traces_dir': str(root / 'traces'), 'max_steps': 2}
            app = WebAgent(settings, store, mock=True)
            session = store.new(workspace=str(root))
            stale = store.get(session.data['id'])
            session.save([UserMessage(content='Web 回合')])
            fresh_messages = []

            def run_loop(messages, *_args, **_kwargs):
                fresh_messages.extend(messages)
                with self.assertRaisesRegex(SessionConflict, '正在运行'):
                    app.conversations.delete_session(session.data['id'])
                with self.assertRaisesRegex(SessionConflict, '正在运行'):
                    app.chat(session, '并发 Web 回合', Mock())
                return True

            runtime = {key: app.runtime[key] for key in ('sessions', 'models', 'tracer', 'tools')}
            runtime['chat_loop'] = run_loop
            with patch.object(cli, 'ensure_config'), patch.object(cli, 'load_config', return_value=settings), \
                 patch.object(cli, 'default_runtime', return_value=runtime), \
                 patch.object(cli, '_pick_session', return_value=stale), patch.object(cli, '_print_banner'), \
                 patch.object(cli.term, 'prepare_pinned_prompt'), patch.object(cli.term, 'clear_pinned_box'), \
                 patch.object(cli.term, 'read_line', side_effect=['CLI 回合', EOFError]):
                cli.repl(mock=True, resume=True)

            self.assertEqual([m.content for m in fresh_messages], ['Web 回合', 'CLI 回合'])
            self.assertEqual(store.get(session.data['id']).messages, fresh_messages)

    def test_stale_writer_cannot_overwrite_or_recreate_session(self):
        with tempfile.TemporaryDirectory() as root:
            store = SessionStore(Path(root))
            session = store.new()
            stale = store.get(session.data['id'])
            session.save([UserMessage(content='latest')])
            with self.assertRaises(SessionConflict):
                stale.save([UserMessage(content='stale')])
            self.assertEqual(store.get(session.data['id']).messages[0].content, 'latest')
            with patch('agent.session.Session.from_path', side_effect=AssertionError('read full history')):
                self.assertEqual(store.summaries()[0]['id'], session.data['id'])
            store.delete(session)
            with self.assertRaises(SessionConflict):
                session.save()

    def test_cli_batch_saves_messages_and_initial_config_creates_directory(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            task = root / 'task.md'
            task.write_text('fixture task', encoding='utf-8')
            folder = root / 'new-config'
            with patch.object(config, '_find_config', return_value=None), patch.object(config, '_default_cfg_path', return_value=folder / 'config.json'):
                self.assertTrue(config.ensure_config().is_file())
            args = cli.build_parser().parse_args(['--mock', '--file', str(task)])
            settings = {'workspace': str(root), 'traces_dir': str(root / 'traces'), 'max_steps': 2}
            with patch('agent.session.SESSIONS_DIR', root / 'sessions'), patch('agent.session.LEGACY_MEMORY', root / 'old-memory.json'), patch.object(cli, 'load_config', return_value=settings):
                cli.run(args)
            session, = SessionStore(root / 'sessions').list()
            self.assertTrue(any('fixture task' in (m.content or '') for m in session.messages))

    def test_trace_payload_is_private_and_delete_removes_session_trace(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            app = WebAgent({'workspace': str(root), 'traces_dir': str(root / 'traces')},
                           SessionStore(root / 'sessions'), mock=True)
            session = app.conversations.new_session()
            tracer = app.runtime['tracer'](session.data['id'])
            tracer.record(1, Action(type='take_note', note_value='private fixture'), 'private output')
            tracer.flush()
            path, = (root / 'traces').glob('*.json')
            self.assertNotIn('private', path.read_text())
            session.save_json_artifact('query.json', {'value': 1})
            app.conversations.delete_session(session.data['id'])
            self.assertFalse(path.exists())
            self.assertFalse(session.artifacts_dir.exists())


class ExecutionBoundaryTests(unittest.TestCase):
    def test_shell_decodes_multibyte_output_before_limiting_and_preserves_tail(self):
        for count in (7_000, MAX_TOOL_OUTPUT + 1):
            with self.subTest(count=count):
                script = f"print('START' + '测' * {count} + 'ERROR'); raise SystemExit(2)"
                output, code = ShellTool()._run([sys.executable, '-c', script])
                self.assertEqual(code, 2)
                self.assertTrue(output.startswith('START'))
                self.assertTrue(output.endswith('ERROR\n'))
                self.assertLessEqual(len(output), MAX_TOOL_OUTPUT)
                if count < MAX_TOOL_OUTPUT:
                    self.assertEqual(output, 'START' + '测' * count + 'ERROR\n')

    def test_cancel_chat_preserves_receipt_and_prevents_tool_execution(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            action = Action(type='call_tool', tool='write_file', tool_call_id='write',
                            args={'path': 'unexpected.txt', 'content': 'fixture'})
            model = MockLLM([action, 'unexpected'])
            app = WebAgent({'workspace': str(root), 'max_steps': 2, 'traces_dir': str(root / 'traces')},
                           SessionStore(root / 'sessions'), llm_factory=lambda: model)
            session = app.conversations.new_session()
            events = []
            def emit(event):
                events.append(event)
                if event['type'] == 'tool':
                    app.conversations.cancel(session.data['id'])
            app.chat(session, '任务', emit, permission_mode='accept')
            self.assertEqual(events[-1], {'type': 'error', 'content': '任务已取消'})
            self.assertFalse((root / 'unexpected.txt').exists())
            restored = app.conversations.session(session.data['id'])
            self.assertEqual(restored.messages[-2].tool_call_id, 'write')
            self.assertEqual(restored.messages[-1].content, '任务已取消')

    def test_scope_blocks_file_symlinks_and_shell_escape(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / 'project'
            project.mkdir()
            outside = Path(root) / 'outside'
            outside.write_text('fixture')
            (project / 'link').symlink_to(outside)
            registry = create_default_registry(workspace=str(project))
            self.assertFalse(registry.execute('read_file', {'path': 'link'}).success)
            self.assertFalse(registry.execute('write_file', {'path': '../outside', 'content': 'changed'}).success)
            action = Action(type='call_tool', tool='shell',
                            args={'command': 'test -f ../outside && exit 17; printf expected-error >&2; exit 2'})
            approver = Mock(return_value=False)
            self.assertEqual(execute_tool(registry, action, approve=approver, mode='ask').status, 'declined')
            approver.assert_called_once()
            result = execute_tool(registry, action, mode='accept')
            self.assertEqual(result.content, '返回码 2\nexpected-error')
            self.assertEqual(outside.read_text(), 'fixture')

    def test_workflow_cancellation_releases_session_without_committing_result(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            app = WebAgent({'workspace': str(root)}, SessionStore(root / 'sessions'))
            session = app.conversations.new_session()
            sid = session.data['id']
            events = []
            def handle(_session, state, _text, _request, progress):
                progress({'type': 'progress', 'content': '第一个阶段'})
                state['unexpected'] = True
                progress({'type': 'progress', 'content': '下一个阶段'})
                return WorkflowReply('unexpected')
            app.conversations.workflows.register('fixture', WorkflowTask('示例', '', '', handle))
            def emit(event):
                events.append(event)
                if event['type'] == 'progress':
                    self.assertTrue(app.conversations.cancel(sid))
                    self.assertEqual(app.conversations.run_status(sid), 'cancelling')
            app.conversations.run_workflow(session, '任务', {'mode': 'fixture'}, emit)
            self.assertEqual(events[-1], {'type': 'error', 'content': '任务已取消'})
            self.assertEqual(app.conversations.run_status(sid), 'idle')
            self.assertNotIn('unexpected', session.data.get('workflows', {}))
