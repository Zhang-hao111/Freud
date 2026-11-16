"""工作流注册、会话状态和回复事件，供交互入口调用。"""

from collections.abc import Callable
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import asdict, dataclass
from typing import Literal

from agent.core import build_chat_system_prompt
from agent.events import Emit, RunCancelled
from agent.session import SessionConflict
from agent.types import AssistantMessage, SystemMessage, UserMessage


@dataclass
class WorkflowReply:
    content: str
    evidence: dict | None = None


@dataclass(frozen=True)
class TaskField:
    name: str
    label: str
    placeholder: str
    kind: Literal['text', 'integer', 'integer-list']
    location: Literal['options', 'request'] = 'options'


@dataclass(frozen=True)
class WorkflowTask:
    label: str
    description: str
    hint: str
    run: Callable
    followup: Callable | None = None
    result_label: str = '结果'
    fields: tuple[TaskField, ...] = ()
    advanced: bool = False
    commands: tuple[str, ...] = ()
    artifacts: tuple[tuple[str, Callable], ...] = ()
    result_llm: object | None = None


class Workflows:
    def __init__(self):
        self.plugins = {}
        self._artifacts = {}
        self._commands = {}

    @contextmanager
    def registration(self):
        """初始化插件失败时撤销该次工作流注册。"""
        registries = (self.plugins, self._artifacts, self._commands)
        snapshots = [registry.copy() for registry in registries]
        try:
            yield
        except Exception:
            for registry, snapshot in zip(registries, snapshots):
                registry.clear()
                registry.update(snapshot)
            raise

    def register(self, name, task: WorkflowTask):
        if name in self.plugins:
            raise ValueError(f'插件已注册：{name}')
        if task.commands and task.followup is None:
            raise ValueError(f'任务不支持结果追问：{name}')
        if len(set(task.commands)) != len(task.commands) or any(command in self._commands for command in task.commands):
            raise ValueError('追问命令已注册')
        artifact_names = [artifact for artifact, _ in task.artifacts]
        if len(set(artifact_names)) != len(artifact_names) or any(artifact in self._artifacts for artifact in artifact_names):
            raise ValueError('产物已注册')
        self.plugins[name] = task
        self._commands.update({command: name for command in task.commands})
        self._artifacts.update(task.artifacts)

    def catalog(self):
        return [{
            'id': name, 'label': task.label, 'description': task.description,
            'hint': task.hint, 'result_label': task.result_label,
            'fields': [asdict(field) for field in task.fields], 'advanced': task.advanced,
        } for name, task in self.plugins.items()]

    def supports_followup(self, name):
        task = self.plugins.get(name)
        return task is not None and task.followup is not None

    def context(self, session):
        context = session.data.get('workflows', {}).get('followup_context')
        if context and self.supports_followup(context.get('target')):
            return context
        return None

    def clear_context(self, session):
        session.data.setdefault('workflows', {})['followup_context'] = None
        session.save()

    def register_followup_commands(self, name, *commands):
        if not self.supports_followup(name):
            raise ValueError(f'任务不支持结果追问：{name}')
        for command in commands:
            if command in self._commands:
                raise ValueError(f'追问命令已注册：{command}')
            self._commands[command] = name

    def register_artifact(self, name, reader):
        if name in self._artifacts:
            raise ValueError(f'产物已注册：{name}')
        self._artifacts[name] = reader

    def artifact(self, session, name, task_id, algorithm=None):
        reader = self._artifacts.get(name)
        return reader(session, task_id, algorithm) if reader else None

    def run(self, session, text, request, emit: Emit, *, checkpoint=None):
        messages = session.messages
        state = deepcopy(session.data.get('workflows', {}))
        connected = True

        def send(event):
            nonlocal connected
            if connected:
                try:
                    emit(event)
                except (BrokenPipeError, ConnectionResetError):
                    connected = False

        def progress(event):
            if checkpoint and event['type'] == 'progress' and event['content'] != '已完成':
                checkpoint()
            send(event)

        try:
            if not messages:
                messages.append(SystemMessage(content=build_chat_system_prompt(session.data['workspace'])))
            session.note_title(text)
            messages.append(UserMessage(content=text, workflow=True))
            session.save(messages)
            mode = request.get('mode')
            followup = mode == 'followup'
            target = (self._commands.get(text.rstrip('。！?？'), request.get('target'))
                      if followup else mode)
            plugin = self.plugins.get(target)
            handler = (plugin.followup if followup else plugin.run) if plugin else None
            if handler is None:
                raise ValueError('未知任务类型')
            if checkpoint:
                checkpoint()
            reply = handler(session, state, text, request, progress)
            if not followup or target != request.get('target'):
                state['followup_context'] = ({'target': target, 'message_index': len(messages) - 1}
                                             if plugin.followup else None)
            use_model = plugin.result_llm is not None and reply.evidence is not None
            if followup and use_model:
                followup_state = state.get('followup_context') or {'target': target}
                state['followup_context'] = followup_state
                start = followup_state.setdefault('message_index', len(messages) - 1)
                recent = [m.content[:500] for m in messages[start:-1] if isinstance(m, UserMessage)][-4:]
                send({'type': 'progress', 'content': '正在根据登记结果回答'})
                try:
                    if checkpoint:
                        checkpoint()
                    reply.content = plugin.result_llm.answer(text, reply.evidence, recent_questions=recent)
                    if checkpoint:
                        checkpoint()
                    reply.content = '**LLM 解读**\n\n' + reply.content
                except RunCancelled:
                    raise
                except Exception:
                    reply.content = '模型暂不可用，以下为登记结果。\n\n' + reply.content
            message = AssistantMessage(content=reply.content, workflow=True)
            session.data['workflows'] = state
            messages.append(message)
            session.save(messages)
            send({'type': 'assistant', 'content': reply.content, 'workflow_result': True})
            if not followup and use_model:
                try:
                    if checkpoint:
                        checkpoint()
                    explanation = plugin.result_llm.answer(text, reply.evidence, summary=True)
                    if checkpoint:
                        checkpoint()
                    interpretation = '**LLM 解读**\n\n' + explanation
                except RunCancelled:
                    send({'type': 'progress', 'content': '已停止解读，任务结果已保存'})
                    return
                except Exception:
                    interpretation = 'LLM 解读暂不可用。'
                heading, _, body = reply.content.partition('\n\n')
                message.content = f'{heading}\n\n{interpretation}\n\n{body}'
                session.save(messages)
                send({'type': 'assistant_update', 'content': message.content})
        except SessionConflict:
            raise
        except Exception as exc:
            try:
                if checkpoint:
                    checkpoint()
            except RunCancelled as cancelled:
                exc = cancelled
            answer = str(exc) if isinstance(exc, RunCancelled) else f'任务未完成：{exc}'
            messages.append(AssistantMessage(content=answer, workflow=True))
            session.save(messages)
            send({'type': 'error', 'content': answer})
