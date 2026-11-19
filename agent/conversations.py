"""应用层会话服务，负责回合、工作区归属和回复事件。"""

from contextlib import contextmanager
from pathlib import Path
from threading import Event, Lock

from agent.core import build_chat_system_prompt
from agent.events import Emit, RunCancelled
from agent.llm import MockLLM
from agent.permissions import MODES
from agent.session import SessionMemory
from agent.tool_execution import record_tool_reply
from agent.tracer import Tracer
from agent.types import Action, AssistantMessage, SystemMessage, UserMessage


class Conversations:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = runtime['sessions']
        self.workflows = runtime['workflows']
        self.workspaces = runtime['workspaces']
        self.mock = runtime['models'].mock
        self.active = {}
        self.lock = Lock()

    def session(self, session_id):
        return self.store.get(session_id)

    def new_session(self, workspace_id='default'):
        workspace = self.workspaces.get(workspace_id)
        if not workspace or not Path(workspace['path']).is_dir():
            raise ValueError('工作区目录不可用，请检查路径')
        return self.store.new(workspace=workspace['path'])

    def session_info(self, session, workspace_ids=None):
        if workspace_ids is None:
            workspace_ids = self.workspace_ids()
        directory = self.workspaces.session_directory(session)
        return {'id': session.data['id'], 'title': session.data['title'],
                'workspace_id': workspace_ids.get(str(directory))}

    def list_sessions(self):
        workspace_ids = self.workspace_ids()
        return [{'id': row['id'], 'title': row['title'],
                 'workspace_id': workspace_ids.get(str(Path(row['workspace'] or self.workspaces.default['path']).resolve()))}
                for row in self.store.summaries()]

    def workspace_ids(self):
        return {row['path']: row['id'] for row in self.workspaces.list()}

    def clear_followup(self, session_id):
        with self.lock:
            if session_id in self.active:
                raise ValueError('该会话正在运行')
            session = self.session(session_id)
            if not session:
                raise ValueError('会话不存在')
            with session.turn():
                self.workflows.clear_context(session)

    def delete_session(self, session_id):
        with self.lock:
            if session_id in self.active:
                raise ValueError('该会话正在运行，无法删除')
            session = self.session(session_id)
            if not session:
                return False
            self.store.delete(session)
            if traces_dir := self.runtime.config.get('traces_dir'):
                Tracer.delete_session(traces_dir, session_id)
            return True

    def cancel(self, session_id):
        with self.lock:
            event = self.active.get(session_id)
            if event is None:
                return False
            event.set()
            return True

    def run_status(self, session_id):
        with self.lock:
            event = self.active.get(session_id)
            return 'idle' if event is None else 'cancelling' if event.is_set() else 'running'

    def checkpoint(self, session_id):
        if self.run_status(session_id) == 'cancelling':
            raise RunCancelled('任务已取消')

    @contextmanager
    def _session_turn(self, session):
        session_id = session.data['id']
        with session.turn():
            with self.lock:
                if session_id in self.active:
                    raise ValueError('该会话正在运行')
                directory = self.workspaces.session_directory(session)
                if not directory.is_dir() or str(directory) not in self.workspace_ids():
                    raise ValueError('请先添加此对话所在的工作区，并确认目录仍然存在。')
                self.active[session_id] = Event()
            try:
                yield str(directory)
            finally:
                with self.lock:
                    self.active.pop(session_id, None)

    def run_workflow(self, session, text, request, emit: Emit):
        if self.mock:
            emit({'type': 'error', 'content': 'Mock 模式不运行数据任务；请去掉 --mock 后重启。'})
            return
        with self._session_turn(session):
            self.workflows.run(session, text, request, emit, checkpoint=lambda: self.checkpoint(session.data['id']))

    def chat(self, session, text, emit: Emit, followup=False, permission_mode='ask', *, approve=None):
        if permission_mode not in MODES:
            raise ValueError('权限模式无效')
        with self._session_turn(session) as workspace:
            def checkpoint():
                self.checkpoint(session.data['id'])

            context = self.workflows.context(session) if followup else None
            if context:
                self.workflows.run(session, text, {'mode': 'followup', 'target': context['target']}, emit,
                                   checkpoint=checkpoint)
            else:
                self._run_chat(session, text, emit, permission_mode, workspace, approve, checkpoint)

    def _run_chat(self, session, text, emit, permission_mode, workspace, approve, checkpoint):
        messages = session.messages
        memory = SessionMemory(session, live_messages=messages)
        tracer = self.runtime['tracer'](session.data['id'])
        try:
            if not messages:
                messages.append(SystemMessage(content=build_chat_system_prompt(workspace)))
            session.note_title(text)
            messages.append(UserMessage(content=text))
            session.save(messages)
            registry = self.runtime['tools'](workspace, checkpoint=checkpoint)
            mock_model = MockLLM([
                Action(type='call_tool', tool='write_file', args={'path': 'mock.txt', 'content': '演示内容'},
                       tool_call_id='mock_1'),
                '这是 Mock 模式的模拟回复。',
            ]) if self.mock else None
            llm = self.runtime['models'].chat(mock_model)

            def handle_action(step, action):
                event = None
                if action.type == 'done':
                    result = action.answer or '完成'
                    event = {'type': 'assistant', 'content': result}
                elif action.type == 'take_note':
                    memory.write(action.note_key or '', action.note_value or '')
                    result = '已记录笔记'
                else:
                    emit({'type': 'tool', 'tool': action.tool, 'args': action.args or {}})
                    checkpoint()
                    outcome = self.runtime['execute_tool'](
                        registry, action, mode=permission_mode, mock=self.mock,
                        approve=approve,
                    )
                    result = outcome.content
                    if outcome.status == 'blocked':
                        result = f'安全护栏: {result}'
                    elif outcome.status == 'declined':
                        result = '用户拒绝或审批超时'
                    elif outcome.status in ('invalid', 'error'):
                        result = f'工具执行异常: {result}'
                    event = {'type': 'result', 'content': result}
                record_tool_reply(messages, action, result)
                tracer.record(step, action, result=result)
                if event is not None:
                    emit(event)

            finished = self.runtime['chat_loop'](
                messages, llm, registry.list(), handle_action,
                lambda reply: emit({'type': 'assistant', 'content': reply}),
                max_steps=self.runtime.config['max_steps'], after_step=memory.consolidate, memory=memory,
                checkpoint=checkpoint,
            )
            if not finished:
                emit({'type': 'error', 'content': '已达到最大执行步数。'})
        except RunCancelled as exc:
            messages.append(AssistantMessage(content=str(exc)))
            emit({'type': 'error', 'content': str(exc)})
        except (BrokenPipeError, ConnectionResetError):
            raise
        except Exception as exc:
            emit({'type': 'error', 'content': str(exc)})
        finally:
            memory.consolidate()
            tracer.flush()


def install_conversations(runtime):
    runtime.provide('conversations', Conversations(runtime))
